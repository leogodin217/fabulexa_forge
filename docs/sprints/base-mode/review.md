# Sprint Review: base-mode

**Date:** 2026-07-23
**Reviewer:** Claude (fresh eyes, tier-2 context loaded) — re-review after `b1fadde` fix commit

Diff base: `8b69a28c2c7ab863a94e339958855d30b6d3dddb` (`git merge-base HEAD design/base-mode`).
Commits reviewed: `8f40756`..`b1fadde` (Phases 1-5 + `b1fadde` "Sprint base-mode - review cleanup").

This is a full re-run of every gate, not a delta review. The prior round
(`docs/sprints/base-mode/review.md` as it stood before this overwrite) found one
blocker in Gate 7: Phase 2 modified `exporters/source/plan.py` and extended
`exporters/slice_only.py` with a shared `omitted_slice_only_columns` helper, in
direct contradiction of the spec's "What Doesn't Change" directive
("`exporters/source/` — untouched… never by refactoring source into a shared
helper"). Commit `b1fadde` reverts `source/plan.py` and `slice_only.py` to their
pre-sprint bodies and moves the omission-scan logic into `exporters/base/plan.py`
as a private `_omitted_slice_only_columns` helper. Verified independently below
(not assumed correct).

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# TODO`/`# Future:` scaffolding, no dead `pass`-only bodies in the sprint diff. The two `+    horizon = DAY_NS` diff lines flagged by the self-rename grep are test-local constant assignments (`horizon` is then passed as an argument), not dead aliases. Every sprint-added public symbol (`BaseConfig`, the four `Base*` errors, `BaseTableSpec`/`BasePlan`, `build_base_plan`, `build_base_render_sql`, `build_base_query_specs`, `export_base`) re-traced via fresh `find_references` calls to a production caller (`cli.py`, `incremental/driver.py`, or `exporters/base/engine.py`) — none dead-ends in tests/demos only. `exporters/source/plan.py` and `exporters/slice_only.py` are now byte-identical to the pre-sprint baseline (`git diff 8b69a28..HEAD` on both files is empty) — confirmed independently, not assumed. |
| 2. Consistency / DRY | observations | 1 | See Findings — the fix reintroduces an exact structural duplicate of `_omitted_slice_only_columns` between `exporters/base/plan.py` and `exporters/source/plan.py`. Flagged per the mechanical DRY heuristic, but recorded as *expected and spec-mandated*, not a defect — see finding. |
| 3. Test names | clean | 0 | Re-read `test_plan.py` (410 lines) and `test_renders.py` (245 lines) in full independently of the prior review's notes; every test name matches its docstring and body (e.g. `test_horizon_reflects_as_of_value_not_the_later_one` asserts `prop__status == "active"` at the horizon, not just row presence; `test_ordered_by_created_sim_time_record_id_over_raw_ns` asserts the literal `ORDER BY` clause text). No test files touched by the fix commit (`git diff 1dbff05..HEAD --name-only` shows only `plan.py`/`slice_only.py`/`source/plan.py`/`review.md`), so this gate's scope for the fix itself is confirming no regression — none found. |
| 4. Test value | clean | 0 | Assertions throughout the re-read files are exact-value (`assert rows["a002"]["deactivated_at"] is None`, `assert order_clause.strip() == '"_base"."created_sim_time", "_base"."record_id"'`), not `len(x) > 0`/weak shapes. No new test multiplication introduced by the fix (no test files changed). |
| 5. Coverage | observations | 1 | Full-suite run: `exporters/base/plan.py` 137 stmts, 99% (2 uncovered: lines 455, 461 — both `raise ExportError(...)` inside `_check_reserved_names` for a renamed **column** colliding with `last_mutation_sim_time` / a reserved column suffix). Same substantive gap as before the fix (line numbers shifted from 429/436 to 455/461 because the reinstated private helper added lines above them) — the fix did not introduce or remove this gap. `exporters/source/plan.py` is 100% (260/260), `exporters/slice_only.py` is 100% (5/5) — both fully covered post-revert. Every other sprint file is 100%. |
| 6. Type-ignore density | clean | 0 | `grep -c "type:\s*ignore"` over the full sprint diff (`src/**/*.py` + `tests/**/*.py`) returns 0. |
| 7. Spec ↔ codebase | observations | 1 | **Prior blocker RESOLVED.** `exporters/source/plan.py` and `exporters/slice_only.py` are now byte-identical to the pre-sprint baseline — confirmed via `git diff 8b69a28..HEAD` on both paths (empty). `exporters/base/plan.py`'s reinstated `_omitted_slice_only_columns` is new code private to `exporters/base/`, built from the spec's own sanctioned reuse surface (`is_non_exempt_slice_only`) rather than a shared cross-module helper — this now matches the spec's "What Doesn't Change" directive exactly: source is untouched, and base "mirrors source's shape by writing new code, never by refactoring source into a shared helper." One pre-existing, non-blocking observation carries over (see Findings) — a spec-summary inaccuracy about `exporters/base/__init__.py`'s docstring, unaffected by the fix. |
| 8. Workspace | clean | 0 | `git status --porcelain` is empty; no untracked files. |
| 9. Lint & typecheck | clean | 0 | `make lint typecheck` → `ruff check .` all-pass, `ruff format --check .` all-formatted, `mypy src` → "Success: no issues found in 111 source files". Additionally ran `pre-commit run --files <all 23 sprint-diff files>` independently (not run by the implementer per protocol) — trim-trailing-whitespace, end-of-file-fixer, ruff (legacy alias), ruff format, mypy (strict, src) all Passed. Also ran `pre-commit run` scoped to just the 3 files the fix touched (`plan.py`, `slice_only.py`, `source/plan.py`) — same all-Passed result. |
| 10. Demos | clean | 0 | All five `docs/sprints/base-mode/demos/phase_{1..5}_*.py` re-run twice each post-fix, exit 0 both times, byte-identical stdout between runs (`diff` empty for all five). |

Full suite: `uv run pytest --cov=src/fabulexa_forge --cov-report=term-missing` → 3753 passed, 18 skipped, 97% total coverage, no regressions.

## Findings

### Gate 2: Consistency / DRY

- **finding 1** (observation): `exporters/base/plan.py:162-184`'s `_omitted_slice_only_columns` and `exporters/source/plan.py:330-353`'s `_omitted_slice_only_columns` are now structurally near-identical (same signature shape, same `sidecar.columns(...)` walk, same `is_non_exempt_slice_only` predicate; the only differences are a local variable name and a module-level prefix constant vs. an inlined literal). Applying the mechanical DRY heuristic in isolation, this is a flaggable duplicate. It is recorded as an *observation, not a blocker*, because the spec's "What Doesn't Change" section explicitly forbids consolidating it: "Base mirrors source's shape by writing new code, never by refactoring source into a shared helper. No extraction of a common plan/render base class." The two modules already shared this exact shape of duplication pre-sprint for other helpers (e.g., each module independently defines its own `_check_collisions`/`_check_reserved_names`/`_slice_only_omission_notice`), so this one additional duplicated private helper is consistent with the established, spec-mandated posture of "same shape, independent code" between `exporters/source/` and `exporters/base/` — not a new smell the fix introduced.

### Gate 5: Coverage

- **finding 1** (observation): `exporters/base/plan.py:454-459` and `:460-464` — `_check_reserved_names`'s two column-level `raise ExportError` branches (a `base.rename` entry producing an output column named `last_mutation_sim_time`, and one producing a name caught by the generic `is_reserved_column_name` check, e.g. `__valid_from_ns`) have zero test coverage. The spec's Phase 2 test list only specifies a **table**-name reserved-name test (`_export_meta`, `*__rows`); no column-name case was ever specified or written, and both existing reserved-name tests in `test_plan.py` (`test_rename_producing_reserved_table_name_raises_export_error`, `test_rename_producing_reserved_rows_suffix_raises_export_error`) hit only the table-name branch. Per the review gate's explicit callout ("uncovered lines that are error conditions… even if 'shouldn't happen'") this should be flagged. Not a blocker — the logic is a straightforward mirror of the already-tested table-name path and of source's identically-shaped, identically-untested column-level branches (`exporters/source/plan.py:881-893`) — but a test would close the gap.

### Gate 7: Spec ↔ codebase

- **RESOLVED** (was blocker in the prior round): The prior finding — Phase 2 modifying `exporters/source/plan.py` and extending `exporters/slice_only.py` with a shared `omitted_slice_only_columns`, contradicting the spec's explicit "exporters/source/ — untouched" directive — is fixed by commit `b1fadde`. Verified independently: `git diff 8b69a28..HEAD -- src/fabulexa_forge/exporters/source/plan.py` and `...exporters/slice_only.py` are both empty (byte-identical to the pre-sprint baseline). `exporters/base/plan.py` now carries its own private `_omitted_slice_only_columns`, built only from the spec's sanctioned reuse surface (`is_non_exempt_slice_only`), exactly mirroring — never sharing — source's equivalent private helper. This is the shape the spec prescribes.
- **finding 1** (observation, carried over from prior round, unaffected by the fix): The spec's "Module Changes Summary" table describes `exporters/base/__init__.py` as gaining "New package docstring **+ layer-direction invariant**." The shipped file is a single-line docstring with no invariant text — but this matches the pre-existing convention of every sibling package's `__init__.py` (`exporters/source/__init__.py`, `exporters/dimensional/__init__.py` are likewise one-liners); the actual "layer-direction invariant" prose lives in each module file's own docstring (`plan.py`, `renders.py`, `engine.py` all state it). A spec-summary inaccuracy, not an implementation defect — the implementation is arguably more consistent with sibling packages than what the spec's summary line implied. Calibration note for the spec process, not a code fix.

## Recommendation

**APPROVED-WITH-NOTES**

The sole blocker from the prior review round (Gate 7: unauthorized modification of
`exporters/source/plan.py` and `exporters/slice_only.py`) is fixed and independently
verified — both files are now byte-identical to the pre-sprint baseline, and the
omission-scan logic lives only as a private, base-local helper mirroring (not
sharing) source's shape, exactly as the spec's "What Doesn't Change" section
requires. All ten gates re-run fresh: no new blockers found. Three observations
remain (the expected/spec-mandated duplicate helper shape, the uncovered
column-level reserved-name branches, and the pre-existing `__init__.py`
spec-summary inaccuracy) — all non-blocking, mergeable as-is, left for the user's
per-item accept/fix decision.
