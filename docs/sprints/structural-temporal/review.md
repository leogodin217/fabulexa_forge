# Sprint Review: structural-temporal

**Date:** 2026-07-26
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | Both new reader symbols (`structural_instant_columns`, `records_structural_column_is_mutable`) have production callers in `validation.py`, `source/renders.py`, `base/renders.py`; `_GRAIN_WINDOW_KEY` deletion confirmed zero-reference; no TODO/pass-only/self-rename patterns in the diff. |
| 2. Consistency / DRY | clean | 0 | New `_GRAIN_CATEGORY` mapping and the three new module-level constants are contract-restatement literals (sanctioned by CLAUDE.md's "same hardcoding class as pinned column lists"), each used from exactly the sites the spec names; sibling files in `reader/`, `exporters/dimensional/`, `exporters/source/`, `exporters/base/` show no pre-existing duplicate of the new helpers. |
| 3. Test names | clean | 0 | Every new/renamed test name matches its body: `test_bogus_category_raises_structure_error`, `test_records_grain_instant_sources_accepted`, `test_record_index_on_records_raises`, etc. all assert exactly what their names claim, including the exception type/message match. |
| 4. Test value | observations | 1 | See Findings — one weak-assertion group in `test_export_dimensional.py`'s new end-to-end test; parametrized tests elsewhere avoid multiplication correctly. |
| 5. Coverage | clean | 0 | `records_columns.py` 100%, `sidecar.py`'s new refusal branch (lines 155-158) fully covered, `base/renders.py` and `source/renders.py` 100%; `validation.py`'s new lines (60-135) are not among its reported missing-line ranges. |
| 6. Type-ignore density | clean | 0 | Zero `# type: ignore` markers added by this sprint's diff. |
| 7. Spec ↔ codebase | clean | 0 | Contracts (`StructuralInstant`, `structural_instant_columns`, `records_structural_column_is_mutable`, the sidecar parse narrowing) match the spec's signatures, docstrings, and Raises clauses verbatim; sprint notes' stated decisions (mutable/set-once as two disjoint frozensets, `_TABLE_CATEGORIES` kept local rather than derived, `_GRAIN_CATEGORY` join table, reuse of the shared `admission` recipe fixture) all check out against the code; no spec-time miss found in gate 7c — the new surface doesn't duplicate anything pre-existing. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty before and after the audit; pre-commit run left the tree clean. |
| 9. Lint & typecheck | clean | 0 | `make lint typecheck` — ruff check, ruff format --check, mypy all pass with zero issues; supplementary `pre-commit run --files <touched files>` also passes clean (all hooks Passed). |
| 10. Demos | clean | 0 | Both `phase_1_structural_surface.py` and `phase_2_records_instants.py` run twice each, exit 0, byte-identical output both times. |

Severity values:
- **clean** — gate found nothing.
- **observations** — gate found smells worth recording but no blocker.
- **blockers** — must fix before merge.

## Findings

### Gate 4: Test value

- **finding 1**: `tests/exporters/dimensional/test_export_dimensional.py::test_records_grain_instant_columns_validate_and_export` (around lines 521-529) asserts `"2024-01-01" in str(deactivated[1])` and `deactivated[2] is not None` / `still_active[3] is not None` rather than exact timestamp values, even though the fixture's exact instants are fully known and constructible from the test's own setup (`created_sim_time=0`, `deactivated_at=50_000_000_000`, `last_mutation_sim_time=50_000_000_000` for `e001`; `last_mutation_sim_time=30_000_000_000` for `e002`, with `runtime.start_datetime = 2024-01-01T00:00:00+00:00`). This is the exact "weak assertion on a deterministic fixture" shape gate 4 flags — `is not None` pins existence, not value, and the substring match pins only the date, not the time-of-day. Severity: observation, not a blocker — the same behavior (including the NULL-close case) is independently pinned with exact values by the `fact-from-records` recipe's `expect.yaml` and by `phase_2_records_instants.py`'s demo assertions, so no behavior is actually unverified project-wide; only this one unit test's own assertions are weaker than they need to be.

## Recommendation

**APPROVED-WITH-NOTES**

No blockers. One observation (Gate 4) recorded for the user's own accept/fix call; it does not block merge and does not require a fix-loop cycle — the underlying behavior it under-asserts is otherwise pinned exactly elsewhere in the sprint's test surface.
