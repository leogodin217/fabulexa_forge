# Data QA round — `data_qa`

Full-round QA of the per-example export configs (base / source / dimensional /
streaming) and the datasets they produce. Branch `data_qa`.

**Method** — the composite QA doctrine, adapted to a reshaper: *script the
generation, agent-judge the data.* Two tiers:

- **Tier A — mechanical gates** (producer-free checkers under `tools/qa/`, read
  only output datasets + the base bundle via `duckdb`/stdlib; never
  `import fabulexa_forge`): `trace_domain` (no-fabrication), `scd2_windows`,
  `refs_resolve`, `determinism`.
- **Tier B — judgment lenses** (11 subagents, one report each in gitignored
  `qa/data_qa/`): a **blind cold-read** (coherence/legibility, output only) and an
  **intent-aware audit** (fidelity vs the config's declared target + the base) per
  example.

Scope: 5 examples × up to 4 modes = 13 export datasets + the streaming replays.

---

## Headline verdict

**The reshape is faithful.** The two reshaper priorities with no producer analogue
both pass across every dataset:

- **No fabrication** — `trace_domain` traced ~500 output columns across 13 datasets;
  every output value is present in its source column's domain. Zero fabricated
  values. (Principle #3 holds.)
- **Referential integrity survived** — every author-declared `fk:` resolves with
  **0 orphans** in every dataset. (Principle #4 holds.)

Plus: **SCD-2 windows sound everywhere** (no inverted/overlapping/duplicate-open
intervals), and **export is deterministic** (2× run, row-for-row identical, all 5
examples).

No finding is a correctness/integrity break in the data. Every open finding is a
**modeling, legibility, or config-comment** issue — i.e. the datasets are *correct*
but, in specific spots, *not as useful or as self-describing as they should be.*

### Tier A gate matrix

| dataset | scd2 | refs | trace (no-fab) | determinism |
|---|---|---|---|---|
| nhs/{base,source,dimensional} | PASS | PASS | PASS | PASS |
| parent-child/base | PASS | PASS | PASS | PASS |
| retail/{base,source,dimensional} | PASS | PASS | PASS | PASS |
| ride-sharing/{base,source} | PASS | PASS | PASS | PASS |
| ride-sharing/dimensional | PASS | FAIL\* | PASS | PASS |
| rs-marketplace/{base,source} | PASS | PASS | PASS | PASS |
| rs-marketplace/dimensional | PASS | FAIL\* | PASS | PASS |

\* Both `refs` FAILs are **false-positives** — see F7. No real dangling reference
exists; every declared `fk:` resolves cleanly.

---

## Findings — classified

Two outcomes per the doctrine: **DROP** (provably correct, or a trivial fix) vs
**FINDING** (open item worth acting on). Biased toward filing.

### F1 — Config comments overclaim "raw sim-time ns" — **DROP (comment fix)**
*Systemic; nhs, retail, ride-sharing, rs-marketplace.*
base mode rebases framework lifecycle columns (`created_sim_time`, `deactivated_at`)
and dimensional rebases `scd_window` columns (`valid_from`/`valid_to`) to wallclock
TIMESTAMP **whenever the sidecar carries a `runtime` anchor**. nhs/retail/rs/rsm all
do; parent-child (anchor ABSENT) correctly stays raw BIGINT. The behavior is
**correct and consistent** with the documented anchor-fallback rule — but my
`base.yaml`/`dimensional.yaml` comments claim "lifecycle timestamps stay raw
sim-time ns," which is only true when no anchor exists. **Fix the comments** in the 4
anchored examples. Evidence: `base.actor.created_sim_time` = `TIMESTAMP 2026-02-01
18:57:40` (rsm) vs source BIGINT; parent-child keeps BIGINT `138728814821`.

### F2 — In-mode timestamp inconsistency (base vs dimensional) — **FINDING (arch/maintainer)**
*Priority: medium.* Within dimensional, `scd_window` columns rebase to TIMESTAMP but
a plain `from: created_sim_time` column stays raw BIGINT — e.g.
`retail.fact_customer_action.occurred_at` is raw ns while the base mode rebases the
same underlying `created_sim_time`. So the *same base column* surfaces as TIMESTAMP
in base and as raw BIGINT in a dimensional fact. Likely intentional (base "presents"
lifecycle; facts keep raw measures) but **undocumented** — worth a forge maintainer
decision + a note in `architecture/anchor.md`.

### F3 — `dim_journey_instance` is an SCD-2 changelog labeled `dim_` — **FINDING (modeling)**
*Systemic; all 4 rich examples. Priority: medium.* Named `dim_` but has non-unique
`id`, many rows/key, `valid_from`/`valid_to` (e.g. rsm 54,573 rows / 8,742 ids). It's
a faithful SCD-2 on `current_state`, but reads as a state-transition fact/history, not
a dimension. Reconsider modeling it as a fact (one row per transition) or renaming.
This is a config choice in our own `dimensional.yaml` files.

### F4 — SCD-2 dims have no surrogate key; facts have no effective-date — **FINDING (modeling)**
*Systemic. Priority: medium.* Facts join versioned dims on the natural `id`, so a
join fans out: nhs `fact_booking` (9,371) × `dim_actor` versions → 26,813 rows. The
star is correct but not point-in-time-joinable as-is. Options: surrogate keys +
effective-dated FKs, Type-1 for join-simplicity where versioning isn't the lesson, or
document the natural-key+effective-date join. May be a forge dimensional-capability
question.

### F5 — retail `fact_customer_action` grain violation — **FINDING (config bug, actionable)**
*retail dimensional. Priority: HIGH.* Declared `key: [id]` (one row per action) but
`id` is **not unique**: 127,543 rows / 123,642 distinct (3,901 dup ids, verified).
The `product_id` FK uses `via: membership`, and the 3,901 actions binding two products
(e.g. `product_comparison`) each fan to 2 rows. All values real (0 orphans) — not
fabrication, but the membership join pushes the fact past its stated grain. **Fix
the config**: drop the multi-valued `product_id`, move it to a bridge table, or
change the declared grain/key.

### F6 — ride-sharing dimensional star is weak (orphaned dims) — **FINDING (config, actionable)**
*ride-sharing dimensional. Priority: HIGH.* `records__actor(trip)` carries no
`ref_index__` columns, so `fact_trip` is a thin 3-column fact with **no FKs**;
`dim_rider`/`dim_zone`/`dim_driver` are orphaned (nothing references them) — only
`dim_journey_instance.actor` links back to `fact_trip.id`. Faithful (no fabrication)
but not a useful star. **Reconsider**: drop the orphaned dims, bridge trips→rider/
zone/driver via journey_instance or a membership edge, or accept that ride-sharing's
value is its CDC/streaming shape, not a star.

### F7 — `refs_resolve` false-positives on business-id columns — **FINDING (tooling)**
*Priority: low.* The checker's `<x>_id` name heuristic flags author business
identifiers (`presentation_id`, `prop__driver_id`, `prop__account_id`) as FKs and
reports orphans, producing the 2 gate FAILs above. No real dangling reference exists.
Refine the checker to read the config and validate only declared `fk:` columns (as
`trace_domain` already parses the config).

### F8 — Opaque raw-BIGINT columns named like timestamps — **FINDING (legibility)**
*nhs, retail. Priority: low.* `requested_at`/`opening_at` (nhs), `occurred_at`
(retail) are raw sim-time ns named like timestamps and not comparable to the rebased
TIMESTAMP SCD windows. Consider a `derived` anchored-timestamp, or a name signaling
raw ns. (Related to F2.)

### F9 — base mode drops surrogate join keys — **DROP (comment fix) + note**
*parent-child; general base-mode behavior.* base strips `ref_index__*` and
`record_index`, so cross-table joins in base output fall back to the opaque `prop__`
business id (which resolves: parent-child `actor.prop__group → entity.id`, 0 dangling).
My parent-child comment touts a `ref_index__group → record_index` surrogate path the
output doesn't preserve. **Fix the comment**; optionally document the general base-mode
behavior.

### F10 — `slice_only` auto-omission under "no exclusions" comments — **DROP (comment fix)**
*parent-child, others.* `journey_instance.state_entry_time`/`complete` are
`slice_only` → auto-omitted with a notice, even where the config comment says "no
exclusions at all." Technically true (no *author* exclusion) but misleading. **Clarify
the comments** that `slice_only` columns are always auto-omitted.

### Non-findings (DROP — provably correct)
- **Zero-variance / all-null columns** (`actor_type='default'`, `resource_type=
  'consultant'`, single-`ops`-actor null PII, empty `label`) — every one mirrors the
  source; faithful, not defects.
- **parent-child not rebased** — correct (no anchor).
- **Terminal `journey_instance` rows** (`active=False`, `created==deactivated`) — a
  base-layer characteristic, inherited, not introduced.

---

## Recommended actions (in priority order)
1. **F5** — fix retail `fact_customer_action` grain (multi-valued product FK).
2. **F6** — rework or scope down ride-sharing's dimensional config.
3. **F1, F9, F10** — correct the overclaiming config comments (trivial, safe).
4. **F3, F4** — decide the journey_instance modeling + SCD-2 join story (may warrant
   a forge maintainer / arch discussion).
5. **F2, F7, F8** — anchor-inconsistency note, refs-checker refinement, timestamp
   legibility (lower priority).

Raw per-agent reports: `qa/data_qa/{coldread,audit}-<example>.md`, `qa/data_qa/gates.md`
(gitignored).
