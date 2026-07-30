# Sprint Review: key-election

**Date:** 2026-07-30
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)
**Diff base:** `f966416` (`enable_better_ids`)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# TODO:`/`# Future:`/stub bodies; every sprint-added public symbol traced via cclsp `find_references` to a production caller in an engine/plan/validation/init module — none dead-ends in tests or demos. |
| 2. Consistency / DRY | observations | 1 | FK surface dispatch is genuinely shared across all four dimensional builders; but the horizon-dispatch helpers are duplicated verbatim between base and source renders. |
| 3. Test names | clean | 0 | Every "raises X" test asserts the exact exception type (usually with `match=`), every byte-identical claim is backed by an equality/substring check, guard tests assert the `(w0)` window label; no name overstates its body. |
| 4. Test value | clean | 2 | No multiplication candidates — near-identical-looking groups each exercise a distinct condition-table path. Assertions are exact-value on tuples/dicts/sets and full error messages, not `len > 0` or bare `isinstance`. |
| 5. Coverage | clean | 0 | New files at 100% (`presentation_key.py`, `populations.py`); `election.py` 99%, base/source `plan.py` 99%, `validation.py` 88%. No file under the 85% bar. TOTAL 98%. |
| 6. Type-ignore density | clean | 1 | Only 5 `# type: ignore[arg-type]` added across the whole diff, in 3 files, max 2 per file — below the ≥3-same-shape-per-file centralize threshold. |
| 7. Spec ↔ codebase | clean | 0 | Both directions checked. `election` threading signatures match the spec's binding amendment exactly; `FkClause.target_key` default change and the `record_id` election default both trace to explicit design-doc text. `election.py` imports no mode package, matching the stated layering invariant. Reverse audit (7c) found no contract the spec would not have proposed had its author read the tree. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty at review time. |
| 9. Lint & typecheck | clean | 0 | `make lint typecheck` exit 0 — ruff check, ruff format --check (320 files), mypy strict (115 source files, no issues). |
| 10. Demos | clean | 0 | All 7 demos, two full passes each: exit 0 both runs, output byte-identical run-to-run. |

## Findings

### Gate 2: Consistency / DRY

- **duplicated horizon-dispatch helpers**: `src/fabulexa_forge/exporters/source/renders.py` defines `_record_index_sql` and `_presentation_key_sql` with the same signature and body as the identical helpers in `src/fabulexa_forge/exporters/base/renders.py` (dispatch `build_X_at_sql` vs `build_X_at_end_sql` on `horizon_ns is None`; only the docstring differs). `_record_index_sql` pre-existed in base only; Phase 5 re-added a copy rather than factoring the horizon-dispatch idiom into `exporters/election.py` or `derivations/`. The Phase 5 sprint note rationalizes recomputing rather than threading from `plan.py` — but that argues against threading, not against sharing the helper itself. Severity: **observation** (correct, tested, small).

### Gate 4: Test value

- **weak assertion on a guard-negative test**: `tests/exporters/dimensional/test_election_fk.py:1095` — `test_guard_restricted_to_own_population_spine_ignores_cross_population_dup` asserts only `any(spec.table_name == "fact_booking" for spec in specs)`. The real claim is "did not raise `ElectedKeyDuplicate`"; an exact row-count/value check on `fact_booking`'s output would pin it harder. Not load-bearing — the no-raise behavior is the point of the test. Severity: **observation**.
- **narrowing boilerplate reads as assertion**: widespread `assert anchor is not None` (e.g. `tests/exporters/source/test_election_renders.py:172`, ~17 occurrences in `tests/exporters/source/test_election_plan.py`) is mypy-strict `Optional` narrowing for `resolve_effective_anchor`, not a real assertion — each is followed by exact-value checks, and the shape matches pre-sprint style. Recorded for completeness, not a gate-4 issue. Severity: **observation**.

### Gate 6: Type-ignore density

- **same-shape ignores below threshold**: `tests/exporters/dimensional/test_populations.py:237,248` and `tests/exporters/dimensional/test_fk.py:386` carry same-shape `# type: ignore[arg-type]` on `source={...}` / `SourceDecl(**kwargs)` literals. Three occurrences across two files is below the centralize threshold, but a small typed builder (mirroring the `SourceDecl(...)` pattern used by `_dim_entity` elsewhere) would remove all three. Severity: **observation**.

## Recommendation

**APPROVED-WITH-NOTES** — no blockers; four observations recorded across gates 2, 4, and 6. Mergeable. Fix-vs-accept on each observation is the user's call at the ACCEPT/FIX checkpoint.

## Observation resolution

The user elected to FIX. Outcome per observation:

| # | Gate | Outcome |
|---|---|---|
| 1 | 2 (DRY) | **FIXED** — `_record_index_sql` / `_presentation_key_sql` moved into `exporters/election.py`; both mode render modules and both mode engines now import the one copy. Verified byte-identical pre-fix bodies, so the move adopted no variant behavior; render and guard still compose literally the same string. `dimensional/populations.py::dim_identity_relation_at_end_sql` checked and confirmed *not* a third copy (it dispatches on `surface`, not `horizon_ns`). |
| 2 | 4 | **FIXED** — the guard-negative test now executes `fact_booking`'s rendered SQL and asserts the exact row `[("b1", "ALPHA_001")]`. |
| 3 | 4 | **DISPUTED / left as-is** — the `assert x is not None` sites are mypy-strict `Optional` narrowing for `resolve_effective_anchor`, each followed by exact-value checks. Removing them fails typecheck; they are not weak assertions. |
| 4 | 6 | **FIXED** — the three same-shape `# type: ignore[arg-type]` replaced with directly-typed `SourceDecl(...)` construction. |

Fix review (2 cycles): cycle 1 returned REVISIONS NEEDED on one finding — `contracts.md`'s module-placement row for `election.py` did not list the `derivations` imports the move introduced, contradicting the layer-direction docstrings the fix itself wrote. Fixed by updating that row while preserving its "never a mode package" prohibition (the load-bearing part, which still holds). Canonical `docs/architecture/` was checked and carries no equivalent stale claim. Cycle 2: **APPROVED**.

Post-fix gates: `make test` 4125 passed / 18 skipped; all 7 demos run twice, exit 0, byte-identical.
