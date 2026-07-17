# Sprint Review: base-format-v6

**Date:** 2026-07-17
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

This is a **re-review**. The prior review (HEAD `1627e29`) found 1 blocker (Gate 10:
`phase_1_taxonomy_posture.py` crashed with `UnsupportedBaseFormatVersionError` because its
inline sidecar was hand-stamped `base_format_version: 5`, which the reader refuses now that
Phase 2 flipped `SUPPORTED_BASE_FORMAT_VERSION` to 6) and 9 non-blocking observations. A fixer
agent rebuilt the demo's inline sidecar at `SUPPORTED_BASE_FORMAT_VERSION` (6) with a real
`record_index` column; this landed in commit `703cbd4` ("Sprint base-format-v6 - review
cleanup"). This review re-runs the full 10-gate process from scratch against the new HEAD — it
does not merely spot-check the fix.

Diff base `8bfeb063f79d76030b96298eefa102f262228a32` (merge-base of `sprint/base-format-v6`
against `parent_branch: revender_for_point_in_time`, per `state.yaml` — unchanged from the
prior review) → HEAD `703cbd4` (was `1627e29`; +1 commit, the fix). 76 files changed (4243
insertions / 1082 deletions) — the delta from the prior review's diff scope is exactly the fix
commit: a 45-line change to `docs/sprints/base-format-v6/demos/phase_1_taxonomy_posture.py`,
plus the (non-code) `review.md` addition itself. Reviewed from the sprint worktree
`/home/leo/projects/worktrees/base-format-v6`.

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | observations | 1 | No `# TODO`/`# Future:` markers, no dead self-rename aliases, no bare-`pass` loop bodies in the sprint's diffed lines. One pre-existing 1-element frozenset membership-test anti-pattern, unchanged from the prior review. |
| 2. Consistency / DRY | observations | 1 | Tier-2 context reloaded for `reader/`, `corrupters/`, `corrupters/operations/`, `exporters/source/`, `exporters/dimensional/` and their tests. No duplicate of a pre-existing helper found. The "co-write the `ref_index__` sibling" block remains duplicated near-verbatim across 3 operation files, unchanged from the prior review. |
| 3. Test names | observations | 1 | All new/changed test bodies re-verified to test what their names claim. One stale `_at_v5`-suffixed test name/docstring survives on a now-v6-stamped fixture, unchanged from the prior review. (The prior review's second Gate-3 observation, on `test_write_emit_records_shape_valid_false_bypasses_shape_net`, is re-classified below under Gate 4 — it is a docstring-claims-untested-interaction issue, which is a test-value concern, not a name-vs-behavior mismatch; recorded once, not double-counted.) |
| 4. Test value | observations | 2 | No test multiplication, no weak assertions (`len(x)>0`, bare `is not None`, chained `==`) found in the diff — grep returned zero matches. One docstring asserts an untested `schema_valid`-independence claim by omission; three tests hand-loop a 2-item tuple instead of parametrizing. Both unchanged from the prior review. |
| 5. Coverage | observations | 2 | Fresh run: 3347 passed, 18 skipped, 0 failed, 97% overall. Sprint-new files (`records_columns.py`, `exporters/source/plan.py`) at 100%. Three defensive `CorruptError` branches uncovered (provably unreachable given caller guards); two C5 type-mismatch branches uncovered, tracing to a spec negative-test-list gap. Both unchanged from the prior review, re-confirmed against the live coverage report and live source, not merely carried forward. |
| 6. Type-ignore density | clean | 0 | One `# type: ignore[index]` in the entire diff — far below any density heuristic. |
| 7. Spec ↔ codebase | observations | 2 | Sprint notes read for all 6 commits in range (the fix commit and the state-only bookkeeping commit carry no note, expected for non-implementation commits). Spec→impl: every re-checked contract (taxonomy, `identity_column`, `SourceUnclassifiedColumn`, `SUPPORTED_BASE_FORMAT_VERSION`, the sentinel rule) matches its docstring against the live code — no blockers, no drift. Impl→spec: the two prior spec-process misses (unreachable validate command in Success Criteria; missing 6th C5 negative test case) independently re-verified as still present, unchanged. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty — no untracked files, nothing uncommitted. |
| 9. Lint & typecheck | clean | 0 | `ruff check .` — all checks passed; `ruff format --check .` — 249 files already formatted; `mypy src` — no issues found in 92 source files. |
| 10. Demos | clean | 0 | **All 4 phase demos now pass, twice each, with byte-identical output across runs.** The prior blocker is confirmed fixed — independently verified against live demo output, not assumed from the fix commit's diff or its commit message. |

## Findings

### Gate 1: Dead Code Scan

- **finding 1** (observation): `src/fabulexa_forge/reader/records_columns.py:31` — `_PRESENTATION_NAMES: Final[frozenset[str]] = frozenset({"presentation_id"})` is a 1-element frozenset module constant used in exactly one membership test (line 59: `if name in _PRESENTATION_NAMES: return "presentation"`). This is the anti-pattern CLAUDE.md/the skill calls out ("1- or 2-element collection literals stored in module-level constants used in exactly one membership test — inline as a tuple literal / direct comparison"). Trivial fix: `if name == "presentation_id": return "presentation"`. Not a correctness issue. Not touched by the fix commit; unchanged from the prior review at the identical location.

  Scan otherwise clean: no `# Future:` / `# TODO:` markers and no bare-`pass` loop bodies in the sprint's diffed lines; no dead self-rename aliases found. Every sprint-added public symbol continues to trace to at least one production (non-test, non-demo) caller.

### Gate 2: Consistency / DRY Check

- **finding 1** (observation): The "co-write the `ref_index__` sibling cell" block is duplicated near-verbatim across three files: `src/fabulexa_forge/corrupters/operations/null_cells.py:197-203`, `dangle_reference.py:238-244`, and `mispoint_reference.py:487-494`. Each follows the shape:
  ```python
  sibling = records_reference_sibling(column, columns_by_name[column])
  if sibling is not None:
      if sibling not in py_columns:
          py_columns[sibling] = population.working_table.data.column(sibling).to_pylist()
      py_columns[sibling][physical_row] = <value>
  ```
  differing only in the assigned value (`None`, `_sentinel_ref_index(sentinel)`, or a freshly-computed `donor_record_index(...)`). A small shared helper would remove the duplication. Not a blocker: each site is correct and tested, and this mirrors a pre-existing lazy-load-then-assign convention already used for each operation's *primary* cell write in these same files (e.g. `null_cells.py:191-195`) — this sprint's new code is consistent with, not a regression from, that convention. Same three locations, same characterization as the prior review; re-confirmed by reading the live files, not carried forward blindly.

  No duplicate of a pre-existing helper was found for any of the sprint's new functions after re-loading tier-2 context across `reader/`, `corrupters/`, `corrupters/operations/`, and `exporters/source|dimensional/`.

### Gate 3: Test Name Audit

- **finding 1** (observation): `tests/exporters/source/test_plan.py:1004` — `test_reference_genre_drops_no_columns_beyond_fork_path_at_v5`. Its docstring says "At v5 the identity index families do not occur", but this file's local `_sidecar()` helper stamps `base_format_version=SUPPORTED_BASE_FORMAT_VERSION` (now 6), and its local `_records_table()` never emits `record_index`/`ref_index__` regardless of version. The fixture is therefore v6-stamped-but-v5-shaped; the assertion itself is correct (`fork_path` dropped, `record_id` kept as `id`), but the name/docstring is stale relative to the version flip. This is the only `_at_v5`-suffixed test name in the tree. Unchanged from the prior review, same location — re-verified against the live file.

  All other new/changed test names checked (records-taxonomy tests, the renamed C5 catalog-recheck tests, the three corrupters' pair-write tests, the minting tests, the never-selectable tests, the identity round-trip test) verified to test exactly what they claim.

### Gate 4: Test Value Audit

- **finding 1** (observation): `tests/_support/test_sidecar_builder.py:240-250` — `test_write_emit_records_shape_valid_false_bypasses_shape_net`'s docstring claims the flag bypasses the shape net "without touching `schema_valid` (still True by default here)", but the body never reads or asserts `sidecar.get("schema_valid")` — it only confirms the missing `record_index` survives the write. The parenthetical claim is asserted by omission, not by an actual check. Unchanged from the prior review, same location.
- **finding 2** (observation): `tests/corrupters/test_selection.py:729-757` — `test_family_a_mutability_predicate_excludes_identity_columns`, `test_nullability_predicate_excludes_identity_columns`, `test_jitter_eligibility_excludes_identity_columns` each hand-loop over the same 2-item `_IDENTITY_COLUMN_NAMES = ("record_index", "ref_index__doctor_id")` tuple rather than using `pytest.mark.parametrize`, inconsistent with this file's parametrize usage elsewhere. Not a blocker — each test exercises a genuinely different predicate, and 2 values is below the "≥3 near-identical bodies" multiplication threshold; a style nit only. Unchanged from the prior review, same location.

  No test-multiplication group and no weak-assertion shape (`len(x) > 0`, bare `is not None`, chained `a == b == c`) found anywhere in the sprint's diffed test bodies — re-confirmed via a fresh grep over the diff, zero matches.

### Gate 5: Coverage Analysis

Fresh run: `uv run pytest --cov=src/fabulexa_forge --cov-report=term-missing` — 3347 passed, 18 skipped, 0 failed, 97% overall (9101 stmts, 250 miss). Sprint-new files `reader/records_columns.py` (24/24) and `exporters/source/plan.py` (238/238) are both 100% covered; no file added this sprint is below 85%.

- **finding 1** (observation): Three "engine invariant" `CorruptError` branches added this sprint remain uncovered:
  - `src/fabulexa_forge/corrupters/state.py:108` — `mint_record_index`'s raise (no captured high-water mark for the target table).
  - `src/fabulexa_forge/corrupters/operations/mispoint_reference.py:189` and `:200` — `donor_record_index`'s two raises (working table absent; no row for the donor id).

  All three are provably unreachable given their call sites: `mint_record_index` is only called on records-category tables, which `CorruptState.__post_init__` always marks; `donor_record_index` is only called after the eligibility loop's own table-presence filter, with a `donor_id` drawn from `resolve_donor_pool`'s scan of that same table. This matches a pre-existing house convention (comparable uncovered defensive branches already exist pre-sprint in `corrupters/operations/_impact.py`). Unchanged from the prior review, same three locations — re-confirmed against the fresh coverage report and the live source, not carried forward blindly.
- **finding 2** (observation): `src/fabulexa_forge/reader/conformance.py:537` and `:555` — in `_check_c5_table`, the type-mismatch branch for the pinned prefix columns (537) and for `record_index`'s own type (555) are uncovered. This traces to a real gap in spec.md's own Phase 2 negative-test list (see Gate 7c finding 2): the five named C5 negatives cover "`ref_index__` of a non-BIGINT type" but not "`record_index` itself of a non-BIGINT type" — a natural sixth case the spec didn't ask for and the implementation correspondingly doesn't cover. Unchanged from the prior review, same two line numbers.

### Gate 6: Type-ignore Density Check

Clean. `git diff <base>..HEAD -- 'tests/**/*.py' 'src/**/*.py' | grep "^+" | grep "type:\s*ignore"` returns exactly one line (`# type: ignore[index]` in `tests/_support/sidecar_builder.py`, on a raw-dict list-index access) — one order of magnitude below the "review at >1 per file" heuristic, nowhere near the "≥3 of the same shape" centralization trigger.

### Gate 7: Spec-Implementation Comparison

**7a — Sprint notes.** Read via `git notes --ref refs/notes/agent/sprint show <sha>` for all 6 commits in the diff range. The fix commit (`703cbd4`) and the state-only bookkeeping commit (`95d44b0`) carry no note, which is expected — neither is an implementation commit. The four phase-implementation commits' notes were re-read and cross-checked against the live code (e.g. Phase 4's eager high-water-mark capture in `CorruptState.__post_init__`, confirmed against `state.py`; Phase 1's decision to keep `record_id` as `id` rather than blanket-dropping identity, confirmed against the source plan's docstring). No new content-level concern found.

**7b — Spec → impl (contracts).** Re-verified against live code (not assumed from the prior review): `records_column_role` / `ref_index_sibling` / `REF_INDEX_PREFIX` (`reader/records_columns.py`), `identity_column` (`tests/_support/sidecar_builder.py:98-126`), `SourceUnclassifiedColumn` (`errors.py:232-241`, subclasses `ExportError` as specified), `SUPPORTED_BASE_FORMAT_VERSION = 6` (`__init__.py:13`), `_sentinel_ref_index`'s `-(n+1)` rule (`dangle_reference.py:101-115`). All match spec.md verbatim. No drift found; no new finding.

**7c — Impl → spec (auditing the spec itself).** Both prior observations independently re-verified as still present and unchanged (spec.md was not touched by the fix commit, so these carry forward by construction — re-checked against the live repo state rather than assumed):

- **finding 1** (observation): spec.md's Success Criterion #1 (`docs/sprints/base-format-v6/spec.md:61-62`) — "`fabulexa-forge validate docs/examples/parent-child/published` passes C1–C13" — cannot be run in a fresh checkout. Re-confirmed: `git check-ignore -v docs/examples/parent-child/published/run.duckdb` shows it's gitignored, and `docs/examples/parent-child/published/` contains only `ATLAS.md` and `base.json`. The example's `base.json` is genuinely v6-shaped (its intent is satisfied), but the literal command in the spec can never be executed to confirm it. Process note for spec-writing: a success criterion should not name a path whose required artifact is excluded from version control.
- **finding 2** (observation): spec.md's Phase 2 test list (`spec.md:324-327`, 5 new C5 negatives) is missing a natural 6th case symmetric with the ones it does list: "`record_index` itself declared a non-BIGINT type". This is the direct cause of Gate 5 finding 2's uncovered branch (`conformance.py:555`) — the implementation faithfully covers exactly what the spec asked for; the spec asked for one fewer case than the check itself actually guards.

### Gate 8: Workspace Check

Clean. `git status --porcelain` returns no output — no untracked files, nothing uncommitted.

### Gate 9: Lint & Typecheck

Clean. `ruff check .` → "All checks passed!"; `ruff format --check .` → "249 files already formatted"; `mypy src` → "Success: no issues found in 92 source files".

### Gate 10: Demo Verification

Clean. All 4 demos (`phase_1_taxonomy_posture.py`, `phase_2_v6_flip.py`, `phase_3_pair_writes.py`, `phase_4_index_minting.py`) were run twice each via `uv run python`; all 8 runs exit 0, and each demo's two runs are byte-identical (diffed explicitly, not assumed from exit codes alone).

`phase_1_taxonomy_posture.py`'s live output was read in full and independently checked, not just its exit code: the taxonomy table correctly classifies `record_index` and `ref_index__location` as `identity`; the source plan's output column list for the widget table (now built with a real `record_index` column in its v6 contract slot) correctly excludes both `record_index` and any `ref_index__*` column; `SourceUnclassifiedColumn` fires correctly for the injected no-role `mystery` column. **The prior review's blocker is confirmed fixed** — the demo no longer hand-stamps `base_format_version: 5`; it now builds its inline sidecar at `SUPPORTED_BASE_FORMAT_VERSION` (6) with a genuine `record_index` column, and `Sidecar.from_raw` accepts it.

The fix commit's 45-line diff was independently re-read against the live file (not just the pasted commit diff) as part of this review — it introduces no new dead-code, DRY, test-value, coverage, or lint/type issue. Phases 2–4 demos continue to pass cleanly and byte-identically, matching their spec.md demo descriptions.

## Recommendation

**APPROVED-WITH-NOTES** — 0 blockers across all 10 gates (Gate 10, the gate that verifies the
prior blocker, is now clean: all 4 demos pass twice with byte-identical output). 9 observations
carried forward from the prior review, each independently re-verified against the live code at
its stated location with no drift in characterization, plus 0 new findings introduced by the fix
commit itself. Per the skill's mechanical mapping (any blocker → REVISIONS NEEDED; else any
observation → APPROVED-WITH-NOTES; else APPROVED), the sprint is mergeable. The 9 observations
are recorded for the user's own per-item accept/fix decision and are not gating.
</content>
