# Sprint Review: source-row-selection

**Date:** 2026-08-13
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

Diff base: `d8eead411e2cae668cfa3bcb137e1ddf75fb9b8e` (parent branch `ci_fixes_and_bugs`).
Sprint commits reviewed: `7e48886` (Phase 1), `4c7f19c` (Phase 2), `ae93844` (Phase 3), `f3c7dda` (Phase 4).

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# TODO`/`# Future:` markers, no bare `pass` bodies, no inert self-renames in the diff; every sprint-added public/private symbol (`cast_predicate_element`, `_narrow_fold_by_spine_sql`, `build_selection_spine_sql`, the four `SourceWhere*` errors, `SourceWhereEntry`, etc.) traces to a production caller. |
| 2. Consistency / DRY | observations | 1 | `plan.py`'s new `_records_bare_property_names` duplicates the pre-existing (already-unreferenced) `columns.py::_scalar_properties` body verbatim; `plan.py` already imports `_PROP_PREFIX` from `columns.py`, so reuse/export was free. |
| 3. Test names | clean | 0 | Sampled `test_where_plan.py` (full, 813 lines), substantial portions of `test_renders.py`, `test_sql.py`, `test_init.py`, `test_source_decls.py`; every name matches its body and error/match assertions. |
| 4. Test value | clean | 0 | Assertions are exact-value (`== N`, `== {...}`, exact message text) throughout; `is not None` uses are Optional-narrowing before further exact assertions (an established repo idiom), not weak pins; the "gate matrix" test groups are spec-directed enumeration over distinct code paths, not multiplication candidates. |
| 5. Coverage | observations | 2 | `plan.py` 95%, `events.py` 99%, `init.py` 95%, `renders.py` 100% over the touched packages (config/models.py, errors.py, exporters/source/*). The only sprint-added uncovered lines: `plan.py`'s `_first_shared_population` exhaustiveness `AssertionError` (idiomatic, not concerning), and `init.py`'s per-sub-type `sub_types` comment line inside the junction name-collision branch (untested combination: collision + sub-typed owner), mirroring a pre-existing identical gap in `_write_state_unit`. |
| 6. Type-ignore density | clean | 0 | Zero new `# type: ignore` markers added by the diff. |
| 7. Spec ↔ codebase | observations | 1 | 7b: every contract (error classes, config model fields/validators, `cast_predicate_element`, `SourceWhereEntry`, plan-builder deltas, render/events seams, `init` deltas) matches the spec's signatures, docstrings, and Raises clauses exactly — no divergence found in either direction. 7c: the spec's Phase 1 contract for `_resolve_where_selection` did not direct reuse of the already-present `_scalar_properties` helper (same finding as Gate 2) — a spec-time miss, not an implementation bug. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty; no stray untracked files. |
| 9. Pre-commit | clean | 0 | `pre-commit run --files <23 changed files>` — trim-trailing-whitespace, end-of-file-fixer, check-yaml, ruff, ruff-format, mypy (strict, src) all Passed. |
| 10. Demos | clean | 0 | All four `docs/sprints/source-row-selection/demos/phase_{1,2,3,4}_*.py` ran twice each, exit 0, byte-identical stdout between runs. |

## Findings

### Gate 2: Consistency / DRY

- **finding 1** (observation): `src/fabulexa_forge/exporters/source/plan.py:1640` (`_records_bare_property_names`) is structurally identical to the pre-existing `src/fabulexa_forge/exporters/source/columns.py:30` (`_scalar_properties`) — both compute `frozenset(col.name[len(_PROP_PREFIX):] for col in sidecar.columns(table) if col.name.startswith(_PROP_PREFIX))`. `columns.py`'s version was already unreferenced before this sprint (confirmed via `find_references`); `plan.py` already imports `_PROP_PREFIX` from `columns.py` at line 98, so importing/exporting the shared helper instead of writing a near-clone was free. Not a correctness issue — pure duplication.
  Fix: consolidate on one definition (export `_scalar_properties` from `columns.py`, or move it there and delete the plan.py copy), consumed by both `plan.py`'s `_resolve_where_selection` and its other pre-existing call sites.

### Gate 5: Coverage

- **finding 1** (observation): `src/fabulexa_forge/exporters/source/init.py:349` — inside `_write_junction_unit`'s `commented` branch, the `if unit.sub_type is not None: w(...)` line is untested. The sprint's only name-collision test (`test_name_collision_comments_out_later_proposal`) uses a flat owner, so the sub-typed-owner + collision combination that this sprint's `sub_type` field introduces is never exercised. Mirrors a pre-existing identical gap in `_write_state_unit` (line 303-304, not introduced this sprint), so this is a consistency gap rather than a novel regression.
- **finding 2** (observation, non-blocking): `src/fabulexa_forge/exporters/source/plan.py:2270` — `_first_shared_population`'s trailing `raise AssertionError(...)` (unreachable given its documented precondition that the caller already confirmed population-set intersection) is uncovered. This is ordinary defensive exhaustiveness for the type checker, not flagged as an anti-pattern, but noted since it is the only uncovered branch `_check_events_source_overlap`'s new helper chain leaves.

### Gate 7: Spec ↔ codebase

- **finding 1** (observation): Same root cause as Gate 2 finding 1 — the spec's Phase 1 contract for the constant-column gate did not call out that `columns.py` already carried an equivalent bare-property-name helper, so the sprint's implementation (faithful to the spec) introduced a duplicate. Recorded here per the skill's 7c calibration note: the audit caught what the spec didn't.

## Recommendation

**APPROVED-WITH-NOTES** — no blockers; three observations recorded above (one DRY duplication, two minor coverage gaps on untested branch combinations). All are mergeable as-is; fix-vs-accept is the user's call.
