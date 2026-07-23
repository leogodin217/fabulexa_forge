# Sprint Review: base-mode

**Date:** 2026-07-23
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

Diff base: `8b69a28c2c7ab863a94e339958855d30b6d3dddb` (`git merge-base HEAD design/base-mode`).
Commits reviewed: `8f40756`..`1dbff05` (Phases 1-5).

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# TODO`/`# Future:` scaffolding, no dead `pass`-only bodies, no inert self-renames in the sprint diff. Every sprint-added public symbol (`BaseConfig`, the four `Base*` errors, `BaseTableSpec`/`BasePlan`, `build_base_plan`, `build_base_render_sql`, `build_base_query_specs`, `export_base`, `omitted_slice_only_columns`) traced via `find_references`/`find_workspace_symbols` to a production caller (cli.py, incremental/driver.py, or a sibling production module) — none dead-ends in tests/demos only. |
| 2. Consistency / DRY | observations | 2 | See Gate 7 for the headline cross-module extraction (reported there since it is primarily a spec-scope violation). Two minor, non-blocking duplications noted below. |
| 3. Test names | clean | 0 | Every test function name in `test_base_config.py`, `test_plan.py`, `test_renders.py`, `test_engine.py`, `test_corrupt_base.py`, `test_base_recipes.py` was checked against its docstring and body; all assert exactly what the name claims (e.g. `test_horizon_reflects_as_of_value_not_the_later_one` does assert the as-of value, not just row presence). |
| 4. Test value | clean | 0 | Assertions are exact-value throughout (e.g. `assert rows["a002"]["deactivated_at"] is None`, `assert row_counts == {"patient": 3}`), not `len(x) > 0`/`is not None` weak shapes. No group of ≥3 tests with bodies differing only in literals; the closest candidates (`test_exclude_kinds_unresolved_raises` / `test_exclude_tables_unresolved_raises`, and the four `mode_section_matches` rejection tests) are pairs/quads with materially different fixture setups, not literal-only variants, and mirror the pre-existing one-test-per-validator-branch convention already used for `dimensional`/`source`. |
| 5. Coverage | observations | 1 | `exporters/base/plan.py` is 99% (2 lines uncovered: 430, 436 — both `raise ExportError(...)` branches inside `_check_reserved_names` for a renamed **column** colliding with `last_mutation_sim_time` / `__valid_from_ns`). Every other sprint-added file (`renders.py`, `engine.py`, the `slice_only.py` addition) is 100%. |
| 6. Type-ignore density | clean | 0 | Zero `# type: ignore` added in `src/` or `tests/` by this sprint's diff. |
| 7. Spec ↔ codebase | blockers | 1 | Phase 2 modified `exporters/source/plan.py` (and extended `exporters/slice_only.py` with a new shared function) to have source delegate its private omission helper to a new mode-neutral one — directly contradicting the spec's explicit "What Doesn't Change" directive. See Findings below. One additional cosmetic spec-accuracy observation. |
| 8. Workspace | clean | 0 | `git status --porcelain` is empty; no untracked files. |
| 9. Lint & typecheck | clean | 0 | `make lint typecheck` → `ruff check` all-pass, `ruff format --check` all-formatted, `mypy src` → "Success: no issues found in 111 source files". |
| 10. Demos | clean | 0 | All five `docs/sprints/base-mode/demos/phase_{1..5}_*.py` ran twice each, exit 0, byte-identical stdout between runs (`diff` empty). |

## Findings

### Gate 2: Consistency / DRY

- **finding 1** (observation): `tests/exporters/base/_base_fixtures.py:53-56`'s `_create_ddl` is a near-verbatim copy of `tests/exporters/source/_source_fixtures.py:147-150`'s `_create_ddl` (both build a `CREATE TABLE` DDL string from a column-dict list). This mirrors a pattern that already exists across the fixture-builder files in this suite (each `_*_fixtures.py` module is self-contained by convention), so it is consistent with the existing test-fixture idiom rather than a new duplication this sprint introduced — noted for awareness only, not a blocker.
- **finding 2** (observation): `exporters/base/plan.py`'s `_state_at_identities(properties, has_pid)` (sorts `properties` alphabetically; used only for the collision/reserved-name scan, order-agnostic) and `exporters/base/renders.py`'s `_state_at_column_order(sidecar, spec)` (walks the sidecar's own column-declaration order; used for actual SQL emission) both compute "the state-at column identities this spec carries," from two different signatures, in two different files. Each has a real reason to differ (the plan-side check is order-agnostic; the render-side one is not), so this is not a clean-cut duplicate to merge, but a reviewer scanning `exporters/base/` cold could reasonably ask why the identity-list logic exists twice. Recorded as an observation, not a blocker.

### Gate 5: Coverage

- **finding 1** (observation): `exporters/base/plan.py:429-439` — `_check_reserved_names`'s two column-level `raise ExportError` branches (a `base.rename` entry producing an output column named `last_mutation_sim_time`, and one producing `__valid_from_ns`) have zero test coverage. The spec's Phase 2 test list only specifies a **table**-name reserved-name test (`_export_meta`, `*__rows`); no column-name case was ever specified or written. Both existing reserved-name tests in `test_plan.py` (`test_rename_producing_reserved_table_name_raises_export_error`, `test_rename_producing_reserved_rows_suffix_raises_export_error`) hit only the table-name branch (line 424). This is an error condition per the review gate's explicit callout ("uncovered lines that are error conditions… even if 'shouldn't happen'"). Not a blocker (the logic is straightforward and mirrors the already-tested table-name path), but should get a test.

### Gate 7: Spec ↔ codebase

- **finding 1** (**BLOCKER**): `src/fabulexa_forge/exporters/source/plan.py:53` (`_omitted_slice_only_columns`) was rewritten to delegate to a new mode-neutral `omitted_slice_only_columns` added to `src/fabulexa_forge/exporters/slice_only.py:34`, and `source/plan.py`'s own inline implementation was deleted (see the diff on both files vs. `8b69a28`). This directly contradicts the sprint spec's "What Doesn't Change" section, verbatim:
  > `exporters/source/` — untouched. Base mirrors source's shape by writing new code, never by refactoring source into a shared helper. No extraction of a common plan/render base class.

  The Phase 2 sprint note records the decision — *"Extracted `omitted_slice_only_columns` to mode-neutral `exporters/slice_only.py` rather than duplicating source's private helper"* — but does not acknowledge or justify overriding the spec's explicit prohibition; it reads as an opportunistic DRY call, not a reasoned deviation. The spec's own Contracts/"reuses… verbatim" language (`exporters/slice_only.py and exporters/notices.py — base reuses is_non_exempt_slice_only, the discriminator carve-out, and the slice-only-column-omitted notice code verbatim`) already gave base a path to the same predicate (`is_non_exempt_slice_only`) without needing to touch `source/plan.py` at all — `build_base_plan`'s own `_surviving_properties` could have implemented the small per-column scan locally (as source's original `_omitted_slice_only_columns` did), exactly mirroring source's shape without editing it. Functionally the change is safe (all tests green, behavior of `source` unchanged) but it is an unauthorized modification of a file the spec explicitly locked as out of scope for this sprint — precisely the kind of quiet contract violation fresh-eyes review exists to catch.
  **Fix:** revert `exporters/source/plan.py` to its pre-sprint body (restore the inline `is_non_exempt_slice_only` scan in `_omitted_slice_only_columns`), and either revert `exporters/slice_only.py`'s new `omitted_slice_only_columns` function or keep it *only* as a base-internal helper (e.g. move it into `exporters/base/plan.py` as a private `_omitted_slice_only_columns`, matching source's original shape) so `exporters/source/` is untouched by this sprint's diff.

- **finding 2** (observation): The spec's "Module Changes Summary" table describes `src/fabulexa_forge/exporters/base/__init__.py` as gaining "New package docstring **+ layer-direction invariant**." The shipped file is a single-line docstring (`"""Base-mode exporter package for fabulexa_forge."""`) with no invariant text — but this exactly matches the pre-existing convention of every sibling package's `__init__.py` (`exporters/source/__init__.py`, `exporters/dimensional/__init__.py` are likewise one-liners); the actual "layer-direction invariant" prose lives in each module file's own docstring (`plan.py`, `renders.py`, `engine.py` all state it). This is a spec-summary inaccuracy, not an implementation defect — the implementation is arguably *better* (consistent with sibling packages) than what the spec's summary line implied. Calibration note for the spec process, not a code fix.

## Recommendation

**REVISIONS NEEDED**

One blocker (Gate 7, finding 1): the sprint's Phase 2 modified `exporters/source/plan.py` and extended the shared `exporters/slice_only.py` module in direct contradiction of the spec's explicit "exporters/source/ — untouched… never by refactoring source into a shared helper" directive. This must be reverted (or the shared helper re-scoped to live only inside `exporters/base/`) before merge. All other findings are observations and do not block merge on their own.
