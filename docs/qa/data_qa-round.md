# Data QA round — `data_qa`

Full-round QA of the per-example export configs (base / source / dimensional /
streaming) and the datasets they produce, followed by a fix pass that resolved every
**config** error. Branch `data_qa`.

**Method** — the composite QA doctrine, adapted to a reshaper: *script the
generation, agent-judge the data.* Two tiers:

- **Tier A — mechanical gates** (producer-free checkers under `tools/qa/`, reading only
  output datasets + the base bundle via `duckdb`/stdlib; never `import fabulexa_forge`):
  `trace_domain` (no-fabrication), `scd2_windows`, `refs_resolve`, `determinism`.
- **Tier B — judgment lenses** (11 subagents, one report each in gitignored
  `qa/data_qa/`): a **blind cold-read** (coherence/legibility, output only) and an
  **intent-aware audit** (fidelity vs declared target + base) per example.

Then a **fix pass** (5 agents) under one governing rule: *fix only authoring errors; if
a fix cannot be expressed in the config grammar, stop and report it as a bug or missing
feature.* No workarounds, no fabricated joins. That rule is what makes the residue below
trustworthy.

---

## Status: all config errors resolved

Final verification on a quiet tree (no concurrent agents):

- `tools/run_all_exports.sh` → **17 configs, 0 failures**
- `tools/qa/run_gates.sh` → **13/13 datasets PASS** (scd2 / refs / trace),
  **determinism PASS** on all 5 examples, **0 failing gate invocations**
  (two consecutive clean full runs)
- `uv run pytest` → 3760 passed, 13 skipped

**Fidelity held throughout.** `trace_domain` traced ~500 output columns across 13
datasets with **zero fabricated values**, and every author-declared `fk:` resolves with
**0 orphans**. Principles #3 (no fabrication) and #4 (integrity preserved) hold.

---

## Resolved — config errors, now fixed

| # | Finding | Resolution |
|---|---|---|
| **F5** | retail `fact_customer_action` grain violation (127,543 rows / 123,642 ids) — a multi-pick binding role wired as a `records`-grain membership FK, which `dimensional.md:438` forbids | Removed `product_id` from the fact (restoring true `key: [id]` → **123,642 / 123,642**) and added `fact_action_product` at `grain: membership`. **93,799** rows = every binding; both compared products preserved (3,901 at `pick_index` 0 **and** 1). |
| **F6** | ride-sharing orphaned dims — thin `fact_trip` with no FKs left `dim_rider`/`dim_zone`/`dim_driver` unreferenced | Bridged with real base-layer edges: `fact_trip_driver` (from `membership__resource__holders`, 1:1), `fact_trip_rider` + `fact_trip_pickup_zone` (from `tick_decision__bindings`). No orphaned dims remain; no join invented. |
| **F4 / F8** | facts not point-in-time joinable — raw BIGINT time columns vs TIMESTAMP SCD windows | `derived: {timestamp: ...}` where a source exists. nhs `fact_booking`→`dim_actor` fan-out **26,813 → 9,371**; →`dim_diary` **7,967,978 → 9,371**. rsm/ride-sharing use `last_mutation_sim_time` (`state_as_of`, `settled_at`). |
| **F1 / F9 / F10** | config comments overclaiming (raw-ns rebase, a surrogate join path base doesn't emit, "no exclusions" vs `slice_only`) | Corrected across every example; each retained claim verified against emitted output. |
| **F7** | `refs_resolve` false-positives on business-id columns | Rewritten config-aware: validates only columns declared `fk: {to:}` against their declared target. **Negative-tested** — deleting referenced `dim_actor` rows makes it FAIL with orphan evidence. |
| — | `trace_domain.py` crashed on non-`records` grains (hardcoded `src.records__<kind>`) | Fixed with a grain→bundle-table mapping. |
| — | `fact_driver.session_earnings` (BIGINT 0–10) named like money | Renamed `session_rides_completed` per source + atlas ("credited as it completes rides"). Value untouched. |

### Dropped — not defects
- **F3 `dim_journey_instance` "mislabeled `dim_`"** — an SCD-2 dimension legitimately has
  one row per version, and `key: [id, valid_from]` is correctly declared. The blind
  cold-readers flagged normal SCD-2 behavior.
- **Zero-variance / all-null columns** — every one mirrors the source; faithful.
- **parent-child not rebased** — correct: its sidecar carries no `runtime` anchor.
- **Terminal `journey_instance` rows** — a base-layer characteristic, inherited.

---

## Open — genuine bugs / missing features

Everything below resisted a config fix. This is the residue.

### R1 — MISSING FEATURE: `derived: timestamp` rejects lifecycle sim-time columns on a `records` grain
*Confirmed independently 4× (nhs, retail, rs-marketplace, ride-sharing).*
```
ERROR: timestamp source 'created_sim_time' is not available on grain 'records' for '<table>.<col>'
ERROR: timestamp source 'deactivated_at'   is not available on grain 'records' for '<table>.<col>'
```
`src/fabulexa_forge/exporters/dimensional/validation.py:104` —
`_TIMESTAMP_SOURCES_BY_GRAIN["records"] = frozenset({"last_mutation_sim_time"})`.

**Asymmetric:** `from: created_sim_time` projects fine as BIGINT, but the same column
cannot be *anchored*. **Consequence:** a records-grain event fact cannot express its own
event/birth time as wallclock, so no point-in-time SCD-2 join is possible for it. For a
short-lived record (rsm `fact_pairing`) only the instant the record *closed*
(`last_mutation` == `deactivated_at` for all 5,109 rows) is reachable — never the instant
it *opened*, which is the natural event time. Membership grain is unaffected
(`joined_sim_time` is accepted). Retail's `occurred_at` remains raw BIGINT as a result;
substituting `last_mutation_sim_time` would be an approximation, so it was not done.

### R2 — MISSING FEATURE: base mode cannot retain a surrogate join key
`BaseConfig` offers only `exclude` / `rename` / `slice_at`; `record_index` and
`ref_index__*` are unconditionally stripped, and `docs/architecture/base.md` does not
mention them. **Consequence:** cross-table joins in base output must use the opaque
`prop__` business id (e.g. parent-child `actor.prop__group → entity.id`, which does
resolve: 0 dangling, 3 honest NULLs).

### R3 — DOC BUG: records-grain projectable-columns list is incomplete
`docs/architecture/dimensional.md:162` omits `created_sim_time`, `presentation_id`, and
`record_index` from the `records` grain surface — all three project successfully and are
used in shipped example configs. The doc is also silent on the R1 asymmetry between the
projection surface and the timestamp-source surface.

### R4 — TOOLING (minor): gate-harness flake under concurrent load
One non-reproducible `trace FAIL` in `run_gates.sh` while `trace_domain.py` standalone
returned `pass: true` on identical inputs; likely a DuckDB attach/lock race in the
harness. Did not reproduce across two clean consecutive full runs. Low priority.

### R5 — PARKED (documented boundary, not a defect)
**Type-2 (as-of) enrichment** — reading a history-tracked property's value as it stood
*during* a row's interval needs a correlated as-of join over `history`; explicitly out of
scope per `dimensional.md` § Boundaries. Not needed by these configs; recorded for
completeness. Likewise **no surrogate dimension keys** — "Dimension keys are the mechanism
`record_id`" is by design.

---

Raw per-agent reports: `qa/data_qa/{coldread,audit}-<example>.md`, `qa/data_qa/gates.md`
(gitignored).
