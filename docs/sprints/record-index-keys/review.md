# Sprint Review: record-index-keys

**Date:** 2026-07-26
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# Future:`/`# TODO:`, no bare `pass` bodies, no self-renames in the diff. Every sprint-added public symbol (`build_record_index_at_sql`, `build_record_index_at_end_sql`, `ReferenceKey`, `NOTICE_REFERENCE_KEY_TARGET_ABSENT`, `RECORD_INDEX_COLUMNS`) has a production caller confirmed via `find_references` (renders.py / plan.py), not just tests or demos. |
| 2. Consistency / DRY | observations | 1 | See Gate 2 below — one documented, spec-justified near-duplicate pattern (not a blocker) and one repeated horizon-selection ternary that mirrors an established idiom rather than inventing a new one. |
| 3. Test names | clean | 0 | Every new/changed test name matches its body: horizon membership, DISTINCT collapse, fork_path filter, no-ORDER-BY, end-of-tape equivalence, notice ordering, rename-collision/reserved-name, and all render-level key scenarios (self key, edge key, dangling, absent, horizon-bound, dedup, rename) were spot-checked line-by-line against their assertions. |
| 4. Test value | observations | 1 | One weak `is not None` assertion in `test_reference_keys.py` where the fixture's exact value is already known and pinned by a sibling test. No test-multiplication (≥3 near-identical bodies) found; every new test in `test_plan.py`/`test_reference_keys.py`/`test_record_index.py` exercises a genuinely distinct scenario with an exact-value assertion. |
| 5. Coverage | clean | 0 | `record_index.py`, `plan.py`, `renders.py` — all sprint-touched/created `src/` files — report 100% line coverage. Full suite: 3822 passed, 18 skipped (pre-existing, unrelated), 0 failed. |
| 6. Type-ignore density | clean | 0 | Zero `# type: ignore` markers added anywhere in the diff. |
| 7. Spec ↔ codebase | clean | 0 | Both directions checked. 7a: sprint notes for both phases (`7caef85`, `1820973`) read; every recorded decision matches the shipped code exactly (rename-default seeding, slice_only shadow-set reuse, absent-target/non-reference fallthrough to `BaseRenameUnresolved`). 7b: every contract in spec.md (`RECORD_INDEX_COLUMNS`, both SQL builders, `ReferenceKey`, `BaseTableSpec.reference_keys`, the notice code, both behavioral deltas) matches signature, docstring, and Raises clause verbatim. 7c: no spec-prescribed helper/constant duplicates an existing one — `_known_records_tables`, `_resolve_reference_keys`, `_key_identities` etc. have no workspace-symbol collision with any pre-existing helper. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty. |
| 9. Lint & typecheck | clean | 0 | `make lint typecheck` — ruff check, ruff format --check, mypy src all pass with no findings. Independently re-verified with `pre-commit run --files <sprint diff files>` — all hooks (trailing-whitespace, end-of-file, check-yaml, ruff, ruff-format, mypy-strict) pass. |
| 10. Demos | clean | 0 | Both `phase_1_record_index_resident.py` and `phase_2_base_keys.py` run twice with identical, deterministic output and exit 0 both times. |

Severity values:
- **clean** — gate found nothing.
- **observations** — gate found smells worth recording but no blocker.
- **blockers** — must fix before merge.

## Findings

### Gate 2: Consistency / DRY

- **finding 1** (observation): `src/fabulexa_forge/exporters/base/renders.py:128-151` (`_record_index_sql`) repeats the exact `X_at_sql if horizon_ns is not None else X_at_end_sql` ternary that `build_base_render_sql` already applies to the state-at pair a few lines below (renders.py:256-260). This is intentional parallelism — the docstring says so explicitly ("the same selection `build_base_render_sql` applies to state-at") — and unifying two 2-line ternaries operating over different builder signatures (state-at needs a `properties` arg; record-index doesn't) would require a higher-order wrapper for no real gain. Not a blocker; noted because the shape is mechanically flaggable as "structurally identical" per Gate 2's grep heuristic. Severity: observation.
- **finding 2** (observation, pre-existing/spec-acknowledged): `src/fabulexa_forge/derivations/truncated_tape.py:241-282` (`_build_ref_index_join`) and the new `renders.py` key-join machinery both LEFT JOIN a reconstructed `prop__<p>` value against a target kind's records table and project `record_index`. The spec's own "What Doesn't Change" section explicitly calls this out and states neither is a template for the other (opposite bound inclusivity: truncated-tape is inclusive at `T`, the new resident is exclusive at a horizon). This is a spec-acknowledged divergence, not a spec-time miss — recorded here only because Gate 2/7c's tier-2 scan surfaces it; no action needed.

### Gate 4: Test value

- **finding 1** (observation): `tests/exporters/base/test_reference_keys.py:269` — `test_renamed_key_columns_appear_under_their_renamed_names` asserts `rows["a001"]["actor_sk"] is not None` for the renamed self key, when the fixture's exact value (`0`, a001 being the first-inserted `actor` row) is already known and is pinned exactly by a sibling test (`test_self_key_never_null` / `test_resolved_edge_key_equals_target_record_index` establish record_index ordering for this fixture). The adjacent assertion in the same test, `rows["a001"]["lead_sk"] == 0`, already demonstrates the exact-value idiom was available. Recommend `== 0` in place of `is not None` for tighter pinning. Not blocking — the test's actual subject (rename honored, old name absent) is still correctly and exactly asserted via `'"actor_key"' not in sql`.
  - Note: `test_self_key_never_null` (line 141) also uses `is not None`, but its named invariant *is* "never NULL" — existence is the property under test, not a stand-in for a missing value check — so it is not flagged.

## Recommendation

**APPROVED-WITH-NOTES** — no blockers; two observations recorded (Gate 2's documented near-duplicate pattern, Gate 4's one weak assertion). Mergeable; the implement-sprint orchestrator does not need to enter a fix loop for these — they are surfaced for the user's own accept/fix decision.
