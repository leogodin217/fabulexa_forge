# Recipes

A **recipe** is a minimal, single-feature export configuration paired with a
compact expectation file. Recipes are the primary author-facing documentation: each
one shows exactly which YAML knobs produce which output shape, with a hand-traceable
row/event count and specific assertions to prove it.

There are four recipe families. Dimensional and source share one config envelope
(`ExportConfig`, run through `fabulexa-forge export`) but not one assertion path — source
needs its own harness because its output tables are sidecar-classification-driven
rather than author-declared, so it gets its own gate file and its own examples
sub-directory rather than living flat alongside the dimensional recipes:

- **Dimensional recipes** — a `mode: dimensional` `ExportConfig`, run through
  `fabulexa-forge export`, asserted against the DuckDB output. They live flat under
  `examples/recipes/<name>/`.
- **Source recipes** — a `mode: source` `ExportConfig` (the *same* envelope
  dimensional uses, just the other `mode` arm), run through `fabulexa-forge export`,
  asserted against the DuckDB output with the same expectation schema dimensional
  uses. They live one level deeper under `examples/recipes/source/<name>/`, gated by
  their own harness (`tests/recipes/test_source_recipes.py`) because `export_source`
  is a different engine entry point than `export_dimensional`.
- **Streaming recipes** — a `StreamConfig`, run through `fabulexa-forge stream`, asserted
  against the per-kind JSONL output. They live one level deeper under
  `examples/recipes/streaming/<name>/`.
- **Corrupter recipes** — a `CorruptConfig`, run through `fabulexa-forge corrupt`, asserted
  against the emitted `defects.json` manifest. They live one level deeper under
  `examples/recipes/corrupt/<name>/`. Unlike the other three families, a corrupter
  recipe declares the *defects* it must produce, not an output shape — see
  authoring guidance in [`.claude/skills/corrupt-config/SKILL.md`](../../.claude/skills/corrupt-config/SKILL.md).

All four families share the same `config.yaml` + `expect.yaml` folder shape.
Dimensional, source, and streaming share one fixture emit (below); the corrupter
family runs against its own, richer fixture — see § Running a recipe and § The recipe
world.

---

## What a recipe is

| File | Purpose |
|---|---|
| `config.yaml` | A complete, commented config — an `ExportConfig` (dimensional and source; `mode:` selects the arm), a `StreamConfig` (streaming), or a `CorruptConfig` (corrupter) |
| `expect.yaml` | Declared output: per-table columns/rows (dimensional and source, same schema), per-stream events (streaming), or defect counts/impact/spot-checks (corrupter, against `defects.json`) |

Every recipe is integration-tested against its fixture emit (see § Running a recipe /
§ The recipe world), so the comments in `config.yaml` are guaranteed to match the real
output. The gates live in `tests/recipes/`: `test_recipes.py` (dimensional),
`test_source_recipes.py` (source), `test_stream_recipes.py` (streaming), and
`test_corrupt_recipes.py` (corrupter — which adds a fourth gate beyond the other
families: the manifest's declared impact codes must set-equal what
`conformance.validate` actually reports on the corrupted output). A recipe folder must
contain exactly `{config.yaml, expect.yaml}`.

---

## Folder layout

```
examples/recipes/
├── dim-scd2-from-records/      ← dimensional recipe (flat)
│   ├── config.yaml             ← commented ExportConfig (mode: dimensional)
│   └── expect.yaml             ← output-table assertions
├── fact-from-history/
│   ├── config.yaml
│   └── expect.yaml
├── source/                     ← source sub-corpus (container, not a recipe)
│   └── source-state-tables/
│       ├── config.yaml         ← commented ExportConfig (mode: source)
│       └── expect.yaml         ← output-table assertions (same schema as dimensional)
├── streaming/                  ← streaming sub-corpus (container, not a recipe)
│   └── state-changes/
│       ├── config.yaml         ← commented StreamConfig
│       └── expect.yaml         ← per-stream event assertions
└── corrupt/                    ← corrupter sub-corpus (container, not a recipe)
    └── null-and-dangle/
        ├── config.yaml         ← commented CorruptConfig
        └── expect.yaml         ← defects.json assertions
```

The discovery harness treats any immediate sub-directory holding a `config.yaml` as a
recipe; a sub-directory without one (e.g. `source/`, `streaming/`, `corrupt/`) is a
container, so all four corpora nest under one tree.

---

## Running a recipe

First build the recipe fixture emit (one-time); the dimensional, source, and
streaming families all run against it:

```bash
uv run python - <<'EOF'
from pathlib import Path
from tests.recipes._recipe_fixture import build_recipe_emit
build_recipe_emit(Path("/tmp/recipe-emit"))
EOF
```

**Dimensional** — `fabulexa-forge export <emit_dir> <config_path> <out> --fmt <csv|duckdb>`:

```bash
fabulexa-forge export \
  /tmp/recipe-emit \
  examples/recipes/dim-scd2-from-records/config.yaml \
  /tmp/dim_scd2.duckdb \
  --fmt duckdb

duckdb /tmp/dim_scd2.duckdb -c "SELECT * FROM dim_patient ORDER BY patient_id, valid_from;"
```

**Source** — same verb as dimensional (`fabulexa-forge export <emit_dir> <config_path> <out>
--fmt <csv|duckdb>`); only `config.yaml`'s `mode: source` selects the other engine
(`export_source` instead of `export_dimensional`), gated by
`tests/recipes/test_source_recipes.py`:

```bash
fabulexa-forge export \
  /tmp/recipe-emit \
  examples/recipes/source/source-state-tables/config.yaml \
  /tmp/source_dump.duckdb \
  --fmt duckdb

duckdb /tmp/source_dump.duckdb -c "SELECT * FROM patient ORDER BY created_at, id;"
```

**Streaming** — `fabulexa-forge stream <emit_dir> <config_path> --fmt jsonl --sink <stdout|file>`:

```bash
mkdir -p /tmp/stream-out
fabulexa-forge stream \
  /tmp/recipe-emit \
  examples/recipes/streaming/state-changes/config.yaml \
  --fmt jsonl --sink file --out /tmp/stream-out

cat /tmp/stream-out/patient.jsonl
```

Exit 0 means the config loaded and the reshape ran clean — the same gate the tests use.
The `--base-date` / `--timezone` rebase overrides are accepted by both verbs.

**Corrupters** — `fabulexa-forge corrupt <emit_dir> --config <config_path> --out <out_dir>`.
The corrupter recipes run against a *different* fixture emit (`build_history_series`,
not `build_recipe_emit` — see § The recipe world), because family C's operations need
a `history` table with real multi-event series to select over:

```bash
uv run python - <<'EOF'
from pathlib import Path
from reader._fixtures_build import build_history_series
build_history_series(Path("/tmp/corrupt-recipe-emit"))
EOF

fabulexa-forge corrupt \
  /tmp/corrupt-recipe-emit \
  examples/recipes/corrupt/null-and-dangle/config.yaml \
  --out /tmp/corrupt-out

cat /tmp/corrupt-out/defects.json
```

Exit 0 is necessary but **not sufficient** here: a `where` filter that matches nothing,
or a `slice_at` boundary an operation's row never crosses, can load and run clean while
declaring zero (or fewer) defects than intended. Check `defects.json`'s
`counts.by_class` and each defect's `impact` against what you meant to break — the
same check `expect.yaml`'s `defect_counts` / `impact_union` encode (§ The expect.yaml
schema). `fabulexa-forge validate /tmp/corrupt-out` cross-checks independently: its failing
checks should union to the same impact codes the manifest declares (minus the
`beyond-c1-c12` sentinel).

---

## The recipe world (fixture contents)

Dimensional, source, and streaming recipes run against the same small deterministic
emit. Everything in the fixture is hand-traceable:

| Table | Rows | Notes |
|---|---|---|
| `records__patient` | 2 | `p001` Alice, `p002` Bob; `prop__status` is history-tracked; `prop__name` is type-1; `prop__doctor_id` → doctor; `prop__primary_staff_id` / `prop__backup_staff_id` → staff (two edges to one kind) |
| `records__doctor` | 1 | `d001` Dr. Carter (created at 50ns — the earliest record) |
| `records__staff` | 2 | `s001` Nora Vega (nurse), `s002` Owen Reed (physician); `prop__staff_type` is the sub-type discriminator |
| `records__admission` | 2 | `a001` active (create only); `a002` deactivated at 2×DAY (create then delete tombstone). Identity + lifecycle kind — the streaming `c`/`d` source |
| `history` | 4 | `patient.status` changes: p001 pending@1×DAY, active@2×DAY, discharged@3×DAY; p002 pending@2×DAY |
| `membership__patient__visits` | 1 | p001 in a morning slot with doctor d001; open interval (`left_sim_time` NULL) |
| `records__queue` | 1 | `q001` Triage — owns the `waiters` collection (the membership-events owner) |
| `membership__queue__waiters` | 2 | waiters on `q001`: p001 priority 2 (joined 1×DAY, left 2×DAY — closed); p002 priority 1 (joined 2×DAY, open). Element fields `elem__priority` + `member__patient__*` |

Patient `prop__primary_staff_id` / `prop__backup_staff_id`: `p001` → primary `s001`,
backup `s002`; `p002` → primary `s002`, backup NULL.

Sidecar extras:

- `runtime`: `timezone=UTC`, `start_datetime=2024-01-01T00:00:00+00:00`
- `pinned_ids`: `{patient: {alice: p001}}`
- `enum_domains`: `{patient: {status: [active, discharged, pending]}, staff: {staff_type: [nurse, physician]}}`
- `record_roles`: `{patient: dimension, doctor: dimension, staff: {nurse: dimension, physician: dimension}, admission: fact, queue: dimension}` — `staff` is sub-typed because its `staff_type` discriminator domain (above) is non-empty (sub-typing follows the `<kind>_type` domain, not the record_roles shape), so a stream's `sub_types` may scope it and its `route_table` leaf is the `prop__staff_type` discriminator value

Time constants are nanosecond offsets from the anchor `2024-01-01T00:00:00Z`:
`1×DAY` = 2024-01-02, `2×DAY` = 2024-01-03, `3×DAY` = 2024-01-04.

**Corrupter recipes run against a different fixture** —
`build_history_series` (`tests/reader/_fixtures_build.py`), not the
`build_recipe_emit` world above. It carries the same table set, records rows,
membership rows, branch/pins/enum_domains/record_roles shape as the base-reader's
spanning fixture, but a richer `history`: at least two distinct change series with
≥2 events each, one series with ≥4 events (so a random `freeze_series` cut has a real
range to land in), and at least one event past `slice_at` (so a defect on a
pre-slice-tracked column still trips C6 rather than the reader's own
"no history row at or before `slice_at`" skip guard). It exists because family C's
operations (`freeze_series` / `drop_events` / `shift_sim_time`) need real series and
event spread to select over — the shared recipe world's `history` is too thin.
`records__actor` also carries one column beyond the base-reader's spanning shape,
`prop__wait_minutes` (BIGINT, history-tracked, backed by its own two-event
`wait_minutes` series, value `12`) — the fixture's only numeric `prop__` column,
added so `duplicate_rows`'s `jitter` (which only perturbs a `prop__`/`elem__` column
typed BIGINT or DOUBLE) has something eligible to target. Build the fixture as shown
in § Running a recipe.

---

## The expect.yaml schema

**Dimensional and source** (`tests/recipes/test_recipes.py` and
`tests/recipes/test_source_recipes.py` — the same expectation schema, since
`export_source` also writes a DuckDB file of named tables):

```yaml
tables:
  <output_table_name>:
    columns: [col_a, col_b, ...]   # exact set (order-insensitive); always required
    row_count: N                    # optional; asserts exact row count
    contains_rows:                  # optional; each entry must match ≥1 output row
      - col_a: value
        col_b: null                 # null means SQL IS NULL
```

The declared table set must equal the exact set of output tables the run produces —
for a source recipe that means every table the config declares (`tables` entries
plus the `events` log when declared); a source recipe declares exactly the tables
under test, since omission is the grammar's exclusion mechanism.

**Streaming** (`tests/recipes/test_stream_recipes.py`):

```yaml
format: jsonl                        # optional; jsonl (default) or debezium
streams:
  <topic>:                         # one entry per emitted <topic>.jsonl stream
    event_count: N                  # optional; asserts exact event (line) count
    contains_events:                # optional; each entry must match ≥1 event
      - seq: 1                      # top-level fields: seq / op / ts / kind
        op: c
        record_id: p001             # matched against key.record_id
        after.prop__status: active  # after.<col> matches one after-image key
        after: null                 # a bare `after: null` asserts the delete tombstone
```

The declared stream-key set must equal the set of emitted `<topic>.jsonl` stems (file
sink), including any topic that emits zero events. The topic is the declared stream's
`name`, verbatim — one `<name>.jsonl` per declared stream (see the Declared-streams
recipes for naming, combining, and sub-type scoping).

`format: debezium` runs the recipe through the Debezium renderer (`--fmt debezium`); the
config must then carry a `debezium` block and resolve an anchor. The output is still one
`<topic>.jsonl` per topic, but each line is a Debezium value message, so `contains_events`
predicates match against the **envelope** (`{schema, payload}` is unwrapped to the
`payload`): top-level `op` and `after` / `after.<col>` as above, plus dotted paths into the
envelope — `before.<col>` (the key-only delete before-image), `source.<col>` (e.g.
`source.table`, `source.lsn`), and `ts_ms`. The jsonl-only `record_id` (key) predicate does
not apply; use `after.record_id` instead. The Kafka sink needs a live broker and so is
covered by integration tests, not the recipe corpus.

**Corrupters** (`tests/recipes/test_corrupt_recipes.py`), asserted against `defects.json`:

```yaml
defect_counts:
  <defect_class>: N        # exact match against defects.json's counts.by_class;
                            # every class the config injects must appear, with its
                            # exact count — no partial declarations
impact_union:
  - C6                      # every non-sentinel ImpactCode the manifest's defects
                            # union to (order-insensitive); 'beyond-c1-c12' is
                            # excluded even when a defect declares it (see the
                            # event-outage-window recipe, whose only impact is that
                            # sentinel, so impact_union: [])
contains_defects:                       # optional; each entry must match ≥1 defect
  - class: missing_value                # defect's top-level `class` field
    rule: null_actor_name               # the operation's `name`, echoed as `rule`
    impact: [C6]                        # exact set match (order-insensitive)
    location.table: records__actor      # dotted traversal into `location`
    location.column: prop__name
```

`defect_counts` and `impact_union` are **exact**, not lower-bounds — a corrupter recipe
declares everything the config must produce, not a sample of it (unlike
`contains_rows` / `contains_events`, which only assert presence). This is what makes
the recipe corpus's fourth gate possible: the recipe is curated so every declared
impact code actually fires on re-validation (no operation that only *sometimes* trips
a check, no operation whose defect the reader's skip-guards silently swallow).

---

## Recipe index

### Dimensional

**Dimensions**

| Recipe | What it teaches |
|---|---|
| [`dim-scd2-from-records`](../../examples/recipes/dim-scd2-from-records/config.yaml) | SCD-2 dimension from a history-tracked `records__<kind>` source; `valid_to: null` identifies the open current version |
| [`dim-scd2-date-window`](../../examples/recipes/dim-scd2-date-window/config.yaml) | `scd_window`'s object form electing a date-grained validity window (`{bound, as: date}`) instead of the default TIMESTAMP bounds; the open version's `valid_to: null` still holds under the election |
| [`dim-type1-from-records`](../../examples/recipes/dim-type1-from-records/config.yaml) | Type-1 (current-snapshot) dimension from a records source with no history tracking; one row per record, no version history |
| [`dim-type1-subtype-split`](../../examples/recipes/dim-type1-subtype-split/config.yaml) | Split one polymorphic kind into per-sub-type Type-1 dimensions via a records-grain `filter` on a discriminator column |

**Facts**

| Recipe | What it teaches |
|---|---|
| [`fact-from-history`](../../examples/recipes/fact-from-history/config.yaml) | Fact table from a `history_point` grain — one row per status-change event in the `history` table |
| [`fact-from-history-interval`](../../examples/recipes/fact-from-history-interval/config.yaml) | Fact table from a `history_interval` grain — one row per state-occupancy interval; the virtual `lead_sim_time` interval end is `NULL` on a series' last (open) interval |
| [`fact-from-membership`](../../examples/recipes/fact-from-membership/config.yaml) | Fact table from a `membership` grain — one row per membership binding; projects `record_id`, `joined_sim_time`, and `elem__*` slot columns |

**Foreign keys & lookups**

| Recipe | What it teaches |
|---|---|
| [`dim-with-fk-reference`](../../examples/recipes/dim-with-fk-reference/config.yaml) | FK reference resolution: denormalize a related record's identity onto a dimension row via the labeled `references` edge (`via: reference`) |
| [`fact-fk-via-membership`](../../examples/recipes/fact-fk-via-membership/config.yaml) | FK resolved through a membership edge (`via: membership`) — denormalize the linked member onto the owner row |
| [`fk-ambiguous-path-hint`](../../examples/recipes/fk-ambiguous-path-hint/config.yaml) | `path` hint that disambiguates a reference FK when multiple edges reach the same target kind |
| [`lookup-reference-property`](../../examples/recipes/lookup-reference-property/config.yaml) | `lookup` column: denormalize a related record's type-1 attribute *value* inline (the FK recipe carries the id; this carries the value) |

**Derived columns**

| Recipe | What it teaches |
|---|---|
| [`derived-value-map`](../../examples/recipes/derived-value-map/config.yaml) | `value_map` derived column: substitute raw status codes with author-supplied display labels; unmapped values become NULL |
| [`derived-ordinal`](../../examples/recipes/derived-ordinal/config.yaml) | `ordinal` derived column: a `ROW_NUMBER()` sequence within a `partition_by`, ordered by a sibling `order_by` (deterministic tie-break) |
| [`derived-timestamp`](../../examples/recipes/derived-timestamp/config.yaml) | `timestamp` derived column: render a raw `sim_time` offset as a wallclock TIMESTAMP via the emit's `runtime` anchor |
| [`derived-timestamp-election`](../../examples/recipes/derived-timestamp-election/config.yaml) | `timestamp`'s `as` election: render the same instant as `date` or `timestamptz` alongside the default TIMESTAMP — the family identity (all three agree on the same wall clock) |
| [`derived-elapsed`](../../examples/recipes/derived-elapsed/config.yaml) | `elapsed` derived column: a cross-row time delta between two correlated events (admission → discharge length of stay) |
| [`derived-elapsed-interval`](../../examples/recipes/derived-elapsed-interval/config.yaml) | `elapsed`'s `as: interval` election: the same cross-row delta rendered as a typed INTERVAL instead of the default unit-divided DOUBLE — exactly one of `unit` / `as` is set |
| [`derived-date-parse`](../../examples/recipes/derived-date-parse/config.yaml) | `date_parse` derived column: declare a VARCHAR source property (an upstream-minted date string, never sniffed) a date in an author-given format, reinterpreted as a real DATE |

**Cross-cutting**

| Recipe | What it teaches |
|---|---|
| [`rebase-timestamp`](../../examples/recipes/rebase-timestamp/config.yaml) | top-level `rebase` block: repin the wallclock origin/zone (`base_date` / `timezone`) that all timestamp columns render through |
| [`table-column-rename`](../../examples/recipes/table-column-rename/config.yaml) | Author-verbatim output naming: rename the table and each column; `from` decouples output names from base-layer columns |
| [`exclude-kinds-tables`](../../examples/recipes/exclude-kinds-tables/config.yaml) | `exclude` guard: declare kinds/tables no output may source; fails loudly on accidental inclusion (it is a guard, not a filter) |

### Source

| Recipe | What it teaches |
|---|---|
| [`source/source-state-tables`](../../examples/recipes/source/source-state-tables/config.yaml) | A `tables` entry with a `kind` address declares the `state` render: one current row per record, unconditionally — a history-tracked kind exports its current values (one row per record, never one per change; CDC-shaped output is streaming's charter) |
| [`source/source-subtype-split`](../../examples/recipes/source/source-subtype-split/config.yaml) | `sub_types: [...]` narrows a state table to a subset of the kind's discriminator domain — one author-named table per sub-type, each single-sub-type table's constant discriminator column dropped (table identity carries it) |
| [`source/source-junction-from-membership`](../../examples/recipes/source/source-junction-from-membership/config.yaml) | A `tables` entry with a `membership` address declares the `junction` render: a faithful read of the interval rows (`<K>_id`, `joined_at`/`left_at`, element/member columns prefix-stripped); an open interval's `left_at` is faithfully null |
| [`source/source-event-log`](../../examples/recipes/source/source-event-log/config.yaml) | The `events` block declares the single polymorphic audit log: `id`/`item_type`/`item_id`/`event`/`occurred_at`/`changes`, one `create`/`update`/`destroy` row per audited change with a JSON `[old, new]` changeset; `id` is the log's own order, dense and `ORDER BY`-able (an update and a destroy at one instant order update first, which `occurred_at` alone cannot express); `item_id` never NULL, even on `destroy` |
| [`source/source-log-only`](../../examples/recipes/source/source-log-only/config.yaml) | A `source:` section declaring only `events` (no `tables`) is a legal, complete config — the audit-stream-only extract; a membership events source audits a junction's fields (`item_type` = `<K>.<property>`, `item_id` = the owner), member references expanding to `<f>_kind`/`<f>_id` entry pairs |
| [`source/source-columns-rename`](../../examples/recipes/source/source-columns-rename/config.yaml) | Per-table `columns` / `rename` narrow and relabel a table's projection — both keyed on source column names (`prop__<p>`, `created_sim_time`), never derived output names; the identity column always projects and is renamed by its elected surface's contract name |
| [`source/source-render-election`](../../examples/recipes/source/source-render-election/config.yaml) | A declared table's unified `render` map elects a structural instant's rendering (`created_sim_time: date`) and, in the same map, declares a payload VARCHAR column a date string (a `date_parse` typed election) — both re-render the projected column in place, keyed on source identity |

### Base

`mode: base` has no declared-table grammar — every records kind exports as one
flat table by default. These recipes exercise `base:`'s escape hatches
(`exclude` / `rename` / `slice_at` / `render`).

| Recipe | What it teaches |
|---|---|
| [`base/base-current-state`](../../examples/recipes/base/base-current-state/config.yaml) | A bare `mode: base` (no `base:` section) is a legal full current-state dump — one flat table per records kind, tape's-end values, no declared-table grammar and no event log |
| [`base/base-exclude-kind`](../../examples/recipes/base/base-exclude-kind/config.yaml) | `base: {exclude: {kinds: [...]}}` drops a kind's table before export — a guard, not a filter; the declared remaining table set proves it |
| [`base/base-rename-table`](../../examples/recipes/base/base-rename-table/config.yaml) | `base: {rename: [...]}` overrides a table's derived default output name; `table` is sidecar identity (`records__<kind>`), never the derived output name |
| [`base/base-slice-at`](../../examples/recipes/base/base-slice-at/config.yaml) | `base: {slice_at: T}` reconstructs every table as of an inclusive point-in-time horizon instead of the tape's end — a tracked property's value as-of T, not its final value |
| [`base/base-render-election`](../../examples/recipes/base/base-render-election/config.yaml) | `base: {render: [...]}` elects a lifecycle-instant's rendering and declares a payload VARCHAR column a date string, per table — the mode's mirror of `rename`'s per-table structure, keyed on the same pre-default column identities |

### Streaming

| Recipe | What it teaches |
|---|---|
| [`streaming/state-changes`](../../examples/recipes/streaming/state-changes/config.yaml) | The canonical CDC stream: a kind's lifecycle as `c`/`u`/`d` events, each carrying a full after-image; type-2 properties spawn a `u` per change while type-1 properties ride every event at the current value |
| [`streaming/identity-tombstone`](../../examples/recipes/streaming/identity-tombstone/config.yaml) | `properties: []` declares an identity-only notification feed — the event set is unchanged (payload-independent); a deactivated record emits a `d` with a null after-image (the log-compaction tombstone) |
| [`streaming/multi-kind-routing`](../../examples/recipes/streaming/multi-kind-routing/config.yaml) | Stream several kinds in one run — one declared stream (and one `<name>.jsonl`) per kind; `seq` is a single global sequence across all streams, so merging the files by `seq` recovers the one true order |
| [`streaming/rebase-ts`](../../examples/recipes/streaming/rebase-ts/config.yaml) | top-level `rebase` block on a stream: choose the wallclock origin/zone each event's `ts` renders through; events and `seq` are unchanged, only `ts` moves |
| [`streaming/clock-realtime`](../../examples/recipes/streaming/clock-realtime/config.yaml) | `clock.mode: realtime` paces delivery to a controlled real-time rate (`speed` = sim-to-real multiplier; `idle_cap_seconds` collapses long quiet gaps); pacing governs only wall-clock timing — bytes, topics, and counts are byte-identical to an unpaced run |

**Declared streams (naming, combining, sub-type scoping)**

| Recipe | What it teaches |
|---|---|
| [`streaming/multi-sub-type-streams`](../../examples/recipes/streaming/multi-sub-type-streams/config.yaml) | A sub-typed kind splits into one topic per sub-type by declaring one stream per sub-type, each `sub_types`-scoped to one discriminator value; there is no implicit split — declaration is the mechanism |
| [`streaming/combined-stream`](../../examples/recipes/streaming/combined-stream/config.yaml) | Several sub-types fold into one declared stream — one topic, one column list — by listing them in `sub_types`; omitting `sub_types` on a sub-typed kind combines the full discriminator domain the same way |
| [`streaming/subtype-select`](../../examples/recipes/streaming/subtype-select/config.yaml) | Stream a subset of a sub-typed kind's sub-types: rows outside the `sub_types` scope drop before the merge (a faithful selection), `seq` numbers only emitted events, and an undeclared sub-type gets no topic — not even a declared-but-empty one |
| [`streaming/custom-stream-name`](../../examples/recipes/streaming/custom-stream-name/config.yaml) | `name` is fully author-chosen (the topic-name rule is the only constraint) — a `cdc.`-prefixed topic is just the `name` string, verbatim; no templating mechanism |

**Membership-events (membership intervals → topics)**

| Recipe | What it teaches |
|---|---|
| [`streaming/membership-events`](../../examples/recipes/streaming/membership-events/config.yaml) | `content: membership-events` streams a collection property's `membership__<owner>__<property>` intervals as an append-only log: each interval unpivots to a `join` (always) and a `leave` (only when the element left — an open interval emits a `join` only). Both carry a full after-image; the owner's identity is the message key; `fields` names bare element-schema fields (`priority` → `elem__priority`; a reference `patient` → `member__patient__kind`/`__id`) |
| [`streaming/membership-identity-only`](../../examples/recipes/streaming/membership-identity-only/config.yaml) | `fields: []` carries owner identity only — the pure join/leave presence signal, no element columns; the membership analog of a state-changes `properties: []` declaration |
| [`streaming/multi-membership-streams`](../../examples/recipes/streaming/multi-membership-streams/config.yaml) | Several membership tables in one run, each under its own author-chosen `name` (independent of the owner/property identity it feeds from); a single global `seq` orders events across all streamed relations |

**Debezium format (`--fmt debezium`)**

| Recipe | What it teaches |
|---|---|
| [`streaming/debezium-state-changes`](../../examples/recipes/streaming/debezium-state-changes/config.yaml) | The state-changes stream as Debezium value messages: the self-describing `{schema, payload}` envelope, the upsert-log `op→before/after` mapping (`c`/`u` carry `before: null`, `d` carries the key-only `before: {record_id}` with `after: null`), the author-set `source` masquerade (`connector`/`name`/`db`/`schema`/`version`) plus derived `lsn`/`table`/`ts_ms`, and the anchor-derived epoch-millisecond `ts_ms` (Debezium requires a resolved anchor) |
| [`streaming/debezium-membership-events`](../../examples/recipes/streaming/debezium-membership-events/config.yaml) | Membership-events as Debezium: the append-only event log renders insert-only (every join *and* leave is envelope `op: c`, `before: null`, `after` never null — no `d`, no tombstone), with the domain op carried as the leading `event` column of the after-image |
| [`streaming/debezium-table-identity`](../../examples/recipes/streaming/debezium-table-identity/config.yaml) | `debezium.table_identity` chooses what the Debezium `source.table` masquerade reports — `source_table` (the per-event `route_table` leaf) or `topic` (the declaring stream's `name`); with a stream named off its leaf the two diverge (`cdc.patient` vs `patient`). Debezium-only; the jsonl format ignores it |

### Corrupters

These run against the `build_history_series` fixture, not the shared recipe world
above (§ The recipe world). Authoring guidance: [`corrupt-config`
skill](../../.claude/skills/corrupt-config/SKILL.md).

**Cell-level defects**

| Recipe | What it teaches |
|---|---|
| [`corrupt/null-and-dangle`](../../examples/recipes/corrupt/null-and-dangle/config.yaml) | Two operations in one config: `null_cells` on an exact `table` + `columns` selector (a missing-value defect, C6), and `dangle_reference` on a membership FK column (a fabricated sentinel id absent from its target table, C10) |
| [`corrupt/mnar-correlated-nulls`](../../examples/recipes/corrupt/mnar-correlated-nulls/config.yaml) | `placement: correlated` — a missing-not-at-random draw biased toward rows where another column equals a given value, instead of the default uniform draw |
| [`corrupt/category-null-cells`](../../examples/recipes/corrupt/category-null-cells/config.yaml) | `target: category: records` — the five-way selector's whole-table-class mode nulls the same column across every `records__*` table in one operation; one table's defect trips C6, another's lands `beyond-c1-c12` (no history tracks that column there) |
| [`corrupt/entity-scoped-placement`](../../examples/recipes/corrupt/entity-scoped-placement/config.yaml) | `placement: entity_scoped` — concentrates a `null_cells` draw onto a seeded subset of distinct `record_id` entities in the pooled population (`category: records` pools two entities here) instead of drawing uniformly over rows |
| [`corrupt/target-glob-and-record-kind`](../../examples/recipes/corrupt/target-glob-and-record-kind/config.yaml) | The two remaining `Target` selector modes: `record_kind` pools every records/membership table of one kind across categories in a single `dangle_reference` (a records FK lands `beyond-c1-c12`, a membership FK lands C10); contrasted with `glob`, which resolves an fnmatch pattern to exactly one table for a `null_cells` op |
| [`corrupt/mispoint-reference`](../../examples/recipes/corrupt/mispoint-reference/config.yaml) | `mispoint_reference`, unconstrained — rewrites a sampled membership FK id to a wrong-but-real donor drawn from the same target table: RI stays green (C10 resolves, C7 stays whole) but the reference points at the wrong row. Declares `beyond-c1-c12` — the flagship subconformance defect, invisible to `validate` and recoverable only via `defects.json` |
| [`corrupt/point-in-time-dangle`](../../examples/recipes/corrupt/point-in-time-dangle/config.yaml) | `mispoint_reference` with `constraint: created_after_reference` — the same mechanism, its donor pool restricted to targets created after the reference's write anchor: the reference resolves *now* but was dangling *at event time* (the late-arriving-dimension scenario). Declares `point_in_time_dangling_reference`, still `beyond-c1-c12` |
| [`corrupt/half-null-pair`](../../examples/recipes/corrupt/half-null-pair/config.yaml) | `null_cells` on one half of a membership C7 pair (`member__<f>__id` alone, its `__kind` partner untouched) — "kind without id," a parameterization of the shipped operation rather than a new mechanism. Trips C7 |

**Cell-value mutations (`mutate_cells`, family A)**

`null_cells` injects missingness; `mutate_cells` injects a *wrong* value via a `kind`-discriminated `mutation` — eleven type-preserving transforms, each gated by the mutated column's declared type. Every mutation is subconformance (`beyond-c1-c12`) except where it lands on the records/`history.value` C6 round-trip or the `records__actor.prop__actor_type` C12 sub-type check.

| Recipe | What it teaches |
|---|---|
| [`corrupt/mutate-sentinel-and-case`](../../examples/recipes/corrupt/mutate-sentinel-and-case/config.yaml) | Two mutation kinds in one config: `sentinel` replaces a stored value with an author-given literal cast into the column's type (a disguised-null defect); `case` applies a case-form transform (`upper`/`lower`/`title`/`swap`) — both land on untracked columns, so both are pure `beyond-c1-c12` |
| [`corrupt/mutate-out-of-domain`](../../examples/recipes/corrupt/mutate-out-of-domain/config.yaml) | `mutation: out_of_domain` on the `prop__actor_type` sub-type discriminator — generates a candidate outside the declared `enum_domains` set, deterministically tripping the C12 sub-type predicate (invisible to `null_cells`-style checks, since `enum_domains` membership is checked by no other C1–C14 check) |
| [`corrupt/mutate-history-value`](../../examples/recipes/corrupt/mutate-history-value/config.yaml) | Targeting `history.value` — the changelog side of the C6 round-trip, new territory `mutate_cells` opened: a `case` mutation on a series' anchor row trips C6 the same way a records-side mutation does |
| [`corrupt/mutate-typo-mnar-placement`](../../examples/recipes/corrupt/mutate-typo-mnar-placement/config.yaml) | `mutation: typo` (adjacent-character exchange, a dirty-join-key defect) composed with `placement: correlated` — the "dirty key, MNAR-placed" pattern; trips C6 on the mutated tracked column |
| [`corrupt/mutate-numeric-scale`](../../examples/recipes/corrupt/mutate-numeric-scale/config.yaml) | `mutation: scale` — a magnitude/unit-scale defect (the "cents mistaken for dollars" scenario) on a `BIGINT` `prop__` column; trips C6 |
| [`corrupt/mutate-string-dirt`](../../examples/recipes/corrupt/mutate-string-dirt/config.yaml) | Two more string mutation kinds: `whitespace` pads a value with a leading/trailing space (always changes a present string; here on an untracked column, `beyond-c1-c12`); `truncate` keeps only the first `max_length` characters (here on a tracked column, tripping C6) |
| [`corrupt/mutate-resample`](../../examples/recipes/corrupt/mutate-resample/config.yaml) | `mutation: resample` — replaces a value with a distinct value drawn from the same column's own donor pool (a "plausible but wrong for this row" defect, structurally distinct from the other kinds' fixed transforms); on `history.value` this trips C6 |

**Row & schema defects**

| Recipe | What it teaches |
|---|---|
| [`corrupt/drift-and-duplicates`](../../examples/recipes/corrupt/drift-and-duplicates/config.yaml) | `duplicate_rows` with a `where` row filter (an exact-duplicate defect, C9) composed with `schema_drift`'s `rename_to` — renaming a tracked column strands its `history` under the old property name, so the rename trips both C11 (the forward-clause pair) and C13 (every record loses its genesis row for the new name) — two operations threaded over the same shared working set |
| [`corrupt/schema-drift-retype-and-drop`](../../examples/recipes/corrupt/schema-drift-retype-and-drop/config.yaml) | One `schema_drift` operation combining `retype_to` and `drop`: a `BIGINT`→`DOUBLE` retype of a tracked column whose round-trip text changes (`"12"` → `"12.0"`) genuinely trips C6, composed with a drop of another tracked column, which trips C11 |
| [`corrupt/duplicate-rows-near-duplicate`](../../examples/recipes/corrupt/duplicate-rows-near-duplicate/config.yaml) | `duplicate_rows` with `jitter`: a near-duplicate row for a pinned record, its numeric `prop__` cell perturbed by a `Distribution` — breaks both the pin-uniqueness check (C9) and the tracked-column round-trip (C6) in one operation |
| [`corrupt/hard-deleted-parents`](../../examples/recipes/corrupt/hard-deleted-parents/config.yaml) | `delete_rows`' referential/pin/history "wake": hard-deleting a referenced doctor row trips C10 (a surviving membership reference now dangles); hard-deleting a pinned, history-tracked actor row trips both C6 (orphaned series) and C9 (broken pin) in one op, evaluated against the post-removal survivor count — composed with `insert_rows` (a bystander phantom keeping `records__actor` non-empty so the pin check isn't vacuous) |
| [`corrupt/phantom-ghost-records`](../../examples/recipes/corrupt/phantom-ghost-records/config.yaml) | `insert_rows` — clones donor rows under fresh, plausible ids (adjacent-character transposition) with `columns` resampled from the donor pool so each phantom doesn't mirror its donor; phantom isolation means no series, reference, or pin is touched, but the phantom carries no `history`, so it lacks its genesis row for the kind's tracked property — the defect declares `C13` |
| [`corrupt/split-brain-duplicates`](../../examples/recipes/corrupt/split-brain-duplicates/config.yaml) | `duplicate_rows`' `mutation` mode — copies a pinned, history-tracked row and `typo`s the copy's name, so two rows now disagree on one identity (the "Jon"/"John" split-brain shape); the conflicting duplicate trips both C6 (the tracked column no longer round-trips) and C9 (the pinned id now resolves to two rows) |

**Family C — `history`'s temporal dimension**

| Recipe | What it teaches |
|---|---|
| [`corrupt/frozen-status-series`](../../examples/recipes/corrupt/frozen-status-series/config.yaml) | `freeze_series` with `cut: after_first` — selects a whole `(kind, record_id, property)` change series (not a cell or row) and suppresses its tail so the value sticks past its true change point (C6) |
| [`corrupt/event-outage-window`](../../examples/recipes/corrupt/event-outage-window/config.yaml) | `drop_events` with `placement: clustered_temporal` — removes sampled `history` events clustered into a narrow `sim_time` window (a simulated outage); the defect's only impact is the `beyond-c1-c12` sentinel, so `impact_union: []` |
| [`corrupt/clock-skew-and-collisions`](../../examples/recipes/corrupt/clock-skew-and-collisions/config.yaml) | `shift_sim_time`'s `kind`-discriminated `ShiftSpec` union: `offset` skews an event's timestamp by a sampled `Distribution`, `collide` forces two events onto the same tick — both break C6 |
| [`corrupt/shift-sim-time-swap`](../../examples/recipes/corrupt/shift-sim-time-swap/config.yaml) | `shift_sim_time`'s third `ShiftSpec`, `swap` — exchanges an event's value with its predecessor tick's, scrambling the value timeline while preserving the tick set; breaks C6 when the swap crosses the series' latest-pre-slice anchor |

**Family E — membership intervals' SCD-2 timeline**

| Recipe | What it teaches |
|---|---|
| [`corrupt/interval-overlap-and-gap`](../../examples/recipes/corrupt/interval-overlap-and-gap/config.yaml) | `distort_intervals`' `overlap` and `gap` modes, composed in one config over one membership table: `overlap` extends an adjacent interval past its successor's join (SCD-2 rebuilt wrong); `gap` shrinks a closed interval into a coverage hole (a lost leave/join message) — its population resolved against `overlap`'s *output*, not the source, the shipped cross-operation composition rule made concrete. Both are pure `beyond-c1-c12` — the subconformance class, invisible to `validate` |
| [`corrupt/inverted-intervals`](../../examples/recipes/corrupt/inverted-intervals/config.yaml) | `distort_intervals`' `left_before_join` mode — swaps a closed interval's `joined_sim_time` and `left_sim_time`, so the member's own recorded interval now ends before it begins. The only C10 timing break besides `dangle_reference`'s |
