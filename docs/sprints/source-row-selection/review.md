# Sprint Review: source-row-selection

**Date:** 2026-08-13
**Reviewer:** Claude (fresh eyes, tier-2 context loaded) — re-review after review-cleanup commit

Diff base: `d8eead411e2cae668cfa3bcb137e1ddf75fb9b8e` (parent branch `ci_fixes_and_bugs`).
Sprint commits reviewed: `7e48886` (Phase 1), `4c7f19c` (Phase 2), `ae93844` (Phase 3),
`f3c7dda` (Phase 4), `5495186` (sprint review), `2d5ffbd` (review cleanup).

This is a re-audit after the `2d5ffbd` cleanup commit, which addressed two of the three
prior observations and left the third as a deliberate, justified accept. All three were
independently re-verified this pass (see "Prior findings — verification" below); all 10
gates were re-run over the full sprint diff, not just the cleanup delta.

## Prior findings — verification

1. **Gate 2 finding 1 (duplicate `_records_bare_property_names`)** — FIXED. `plan.py`'s
   private clone is deleted; all four of its former call sites (`plan.py:1218`, `:1672`,
   `:2099`, plus the import at `:98`) now call `columns.py::_scalar_properties` directly
   (confirmed via `find_references` — 4 call sites, no residual dead alias). Bodies were
   byte-identical before the fix, so this is a pure consolidation, not a behavior change.
2. **Gate 5 finding 1 (`init.py` sub-typed-owner junction collision branch uncovered)** —
   FIXED. New test `test_subtyped_junction_name_collision_comments_out_proposal` (+
   `_build_subtyped_junction_collision_emit`, both reusing pre-existing fixture constants
   `_UNTRACKED_FLAT_COLUMNS` / `_SUBTYPED_TRACKED_ACTOR_COLUMNS` / `_SUBTYPED_TRACKED_ACTOR_ROW`
   / `_OWNER_MEMBERSHIP_COLUMNS`, not new literals) exercises `_write_junction_unit`'s
   `commented` branch with a sub-typed owner. Coverage confirms line 349
   (`if unit.sub_type is not None: w(...)`) is no longer in `init.py`'s missing set;
   `init.py` is 96% (up from 95%), remaining gaps (300-305, 377) both pre-date the sprint
   (blamed to `68ff0444` / `3379c99d`, both before the 2026-08-12 sprint start).
3. **Gate 5 finding 2 (`_first_shared_population` `AssertionError`)** — left as-is, as
   directed. Re-confirmed: `plan.py:2251`, blamed to sprint commit `ae93844` (Phase 3),
   remains the sprint's only uncovered line. It is a `for`-loop exhaustiveness terminator
   required for the function's declared `-> Population` return type to type-check under
   mypy-strict, not "validating internal inputs" — the Over-Engineering anti-pattern row
   doesn't apply, and it is not reachable given the caller's precondition. Accepted.

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# TODO`/`# Future:` markers in the full sprint diff; no bare-`pass` bodies; `_records_bare_property_names` is fully removed (zero references) and `_scalar_properties` now has 4 live call sites in `plan.py`, confirming genuine consolidation rather than a second dead symbol. |
| 2. Consistency / DRY | clean | 0 | The plan.py/columns.py duplication is resolved; no new duplication introduced by the cleanup commit's test additions (the new emit-builder reuses existing fixture constants, not new literals). |
| 3. Test names | clean | 0 | `test_subtyped_junction_name_collision_comments_out_proposal`'s body matches its name and docstring exactly — asserts the commented junction stub retains its `sub_types: [consultant]` line. Full sprint diff resampled; no name/body mismatch found. |
| 4. Test value | clean | 0 | New test uses exact-value string-containment assertions against the full expected commented block (kind, membership, `sub_types` line) — not a weak existence check. Not a multiplication candidate: it exercises a genuinely distinct branch (sub-typed owner) from the sibling flat-owner collision test. |
| 5. Coverage | observations | 1 | `plan.py` 95%, `events.py` 99%, `init.py` 96% (up from 95%), `renders.py` 100%, `config/models.py` 99%, `errors.py`/`_sql.py` 100% over the touched packages. The sole sprint-added uncovered line is `plan.py:2251`'s `_first_shared_population` exhaustiveness `AssertionError` — reconfirmed idiomatic, accepted per the cleanup decision. |
| 6. Type-ignore density | clean | 0 | Zero `# type: ignore` markers added anywhere in the full sprint diff. |
| 7. Spec ↔ codebase | clean | 0 | 7b: contracts (error classes, config model fields/validators, `cast_predicate_element`, `SourceWhereEntry`, plan-builder deltas, render/events seams, `init` deltas) match the spec's signatures, docstrings, Raises clauses. 7c: the prior 7c finding (spec didn't direct reuse of `_scalar_properties`) is now moot — the cleanup commit performed the reuse the spec should have prescribed, closing the gap the audit caught. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty; no stray untracked files. |
| 9. Pre-commit | clean | 0 | `pre-commit run --files <24 sprint-changed files>` (excluding `review.md` itself) — trim-trailing-whitespace, end-of-file-fixer, check-yaml, ruff, ruff-format, mypy (strict, src) all Passed. |
| 10. Demos | clean | 0 | All four `docs/sprints/source-row-selection/demos/phase_{1,2,3,4}_*.py` ran twice each, exit 0, byte-identical stdout between runs. |

Full suite: `4423 passed, 18 skipped` (`uv run pytest -q`, full repo).

## Findings

### Gate 5: Coverage

- **finding 1** (observation, non-blocking): `src/fabulexa_forge/exporters/source/plan.py:2251` — `_first_shared_population`'s trailing `raise AssertionError("caller already confirmed the population sets intersect")` remains uncovered. This is `for`-loop exhaustiveness for the declared `-> Population` return type under mypy-strict, guarded by the caller's precondition (`_check_events_source_overlap` only calls it after confirming both `_events_share_item_space` and a failed `_common_disjoint_where_column` check). Re-verified this pass and left unchanged per the cleanup decision; not re-flagged as a blocker.

## Recommendation

**APPROVED-WITH-NOTES** — no blockers; one observation recorded above (the same,
previously-accepted defensive `AssertionError`, re-verified unchanged). The other two
prior observations are fixed. Mergeable as-is.
