# Sprint Review: base-format-v6

**Date:** 2026-07-17
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

Diff base `8bfeb063f79d76030b96298eefa102f262228a32` (merge-base of `sprint/base-format-v6`
against `parent_branch: revender_for_point_in_time`, per `state.yaml`) → HEAD `1627e29`
(Phase 4 complete). 75 files changed (4136 insertions / 1082 deletions). Reviewed from the
sprint worktree `/home/leo/projects/worktrees/base-format-v6`.

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | observations | 1 | No scaffolding/TODOs/dead aliases in the diff; every new public symbol has a production caller. One pre-existing module-constant anti-pattern (1-element frozenset membership test) in new code. |
| 2. Consistency / DRY | observations | 1 | Tier-2 context loaded for `reader/`, `corrupters/`, `corrupters/operations/`, `exporters/source/`, `exporters/dimensional/`. No duplicate of a pre-existing helper found. The new "co-write the `ref_index__` sibling" block is near-identical across 3 operation files. |
| 3. Test names | observations | 2 | All new/changed test bodies verified to test what their names claim, including the renamed C5 tests tracking the catalog-recheck removal. Two docstring-precision nits. |
| 4. Test value | observations | 1 | No test multiplication, no weak assertions — v6-specific tests pin exact values throughout (dangle sentinel, minted indices, bare `{name,type}` dicts). One minor parametrize-style nit. |
| 5. Coverage | observations | 2 | New file `records_columns.py` 100% covered; `plan.py` 100%. Three new defensive `CorruptError` branches uncovered (provably unreachable given caller guards). One C5 type-mismatch branch uncovered, tracing to a gap in the spec's own negative-test list. |
| 6. Type-ignore density | clean | 0 | One `# type: ignore[index]` added in the entire diff — far below any density heuristic. |
| 7. Spec ↔ codebase | observations | 2 | Sprint notes read for all 4 phase commits. Spec→impl: every contract (taxonomy, `SourceUnclassifiedColumn`, `identity_column`, amended C5, pair-write corrupters, index minting, jitter exclusion) matches its docstring and behaves as demoed — no blockers found. Impl→spec: one success criterion is unverifiable as literally stated; one negative-test gap. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty — no untracked files. |
| 9. Lint & typecheck | clean | 0 | `make lint typecheck`: ruff check clean, ruff format clean (249 files), mypy clean (92 source files). |
| 10. Demos | **blockers** | 1 | Phase 1's demo crashes deterministically on both runs. Phases 2–4 demos pass and are byte-identical across two runs. |

## Findings

### Gate 1: Dead Code Scan

- **finding 1** (observation): `src/fabulexa_forge/reader/records_columns.py:31,59-60` — `_PRESENTATION_NAMES: Final[frozenset[str]] = frozenset({"presentation_id"})` is a 1-element frozenset module constant used in exactly one membership test (`if name in _PRESENTATION_NAMES: return "presentation"`). This is the exact anti-pattern CLAUDE.md/the skill calls out ("1- or 2-element collection literals stored in module-level constants used in exactly one membership test — inline as a tuple literal / direct comparison"). Trivial fix: `if name == "presentation_id": return "presentation"`. Not a correctness issue.

  Scan results otherwise clean: no `# Future:` / `# TODO:` markers, no bare-`pass` loop bodies, no dead self-rename aliases (the two candidate `+   x = y` diff lines, `record_index_idx = prefix_len` and `block_start = record_index_idx`, are genuinely branched/reused, not dead aliases — verified by reading the surrounding `_check_c5_table`). Every new public symbol (`records_column_role`, `ref_index_sibling`, `REF_INDEX_PREFIX`, `RecordsColumnRole`, `SourceUnclassifiedColumn`, `identity_column`, `records_reference_sibling`, `donor_record_index`, `mint_record_index`) traced via `find_references`/direct reading to at least one production (non-test, non-demo) caller.

### Gate 2: Consistency / DRY Check

- **finding 1** (observation): The "co-write the `ref_index__` sibling cell" block is duplicated near-verbatim across three files: `src/fabulexa_forge/corrupters/operations/null_cells.py:197-203`, `dangle_reference.py:238-243`, and `mispoint_reference.py:487-493`. All three share the shape `sibling = records_reference_sibling(...); if sibling is not None: (cache column into py_columns if absent); py_columns[sibling][physical_row] = <value>` — differing only in the assigned value expression (`None`, `_sentinel_ref_index(sentinel)`, or a freshly-computed `donor_record_index(...)`). A small shared helper (e.g. `_write_sibling_cell(py_columns, population, sibling, physical_row, value)`) would remove the duplication. Not a blocker: each site is correct and tested (confirmed via the Phase 3 demo and the pair-write test suites), and this mirrors a **pre-existing** style choice already present in these same files — the primary-column cache-into-`py_columns` pattern each operation writes for its own column was never factored into a shared helper either, before this sprint. This sprint's new code is consistent with, not a regression from, that convention.

  No duplicate of a pre-existing helper was found for any of the sprint's new functions (`records_column_role`, `ref_index_sibling`, `identity_column`, `records_reference_sibling`, `donor_record_index`, `_table_record_index_mark`/`mint_record_index`, `_require_all_columns_classified`, `_sentinel_ref_index`) after reading the sibling modules in `reader/`, `corrupters/`, `corrupters/operations/`, and `exporters/source|dimensional/`. `write_emit`'s new shape assertion correctly reuses the reader's own `_check_c5_table` rather than re-implementing the positional check (an explicit, sound decision recorded in the Phase 2 sprint note). `insert_eligible_columns` / `is_mutable_column` (pre-existing, untouched) already exclude `record_index`/`ref_index__*` structurally (they only ever select `prop__*`/`presentation_id`/`elem__*`/`value`), so Phase 4's "never-selectable" invariants needed no new production code, matching the spec's own "What Doesn't Change" framing.

### Gate 3: Test Name Audit

- **finding 1** (observation): `tests/exporters/source/test_plan.py:1004` — `test_reference_genre_drops_no_columns_beyond_fork_path_at_v5`. Its docstring says "At v5 the identity index families do not occur", but `_sidecar()` (this file's local helper) stamps `base_format_version=SUPPORTED_BASE_FORMAT_VERSION`, which is now 6 — and `_records_table()` (also local to this file, distinct from `_support/sidecar_builder`) never emits `record_index`/`ref_index__` regardless. So the fixture is a v6-stamped-but-v5-shaped hybrid that neither `Sidecar.from_raw` nor `build_source_plan` structurally rejects (only `write_emit`'s construction-time net and the read-time C5 check do that, and neither runs in this file). The assertion itself is correct (`fork_path` dropped, `record_id` kept as `id`); the name/docstring is just stale relative to the version flip. Harmless — this file's fixtures were correctly left un-migrated per spec's "existing `test_plan.py` assertions still pass, unmodified", but the new test added *this sprint* inherited the "at_v5" framing without noting the stamped version moved.
- **finding 2** (observation): `tests/_support/test_sidecar_builder.py:240` — `test_write_emit_records_shape_valid_false_bypasses_shape_net`'s docstring claims the flag bypasses the shape net "without touching `schema_valid` (still True by default here)", but the body never exercises that interaction (e.g. a table that is simultaneously schema-invalid to confirm `schema_valid` still fires independently) — it only confirms the missing `record_index` survives the write. The parenthetical is asserted by omission, not by an actual check.

  All other new/changed test names in the priority set (`test_records_columns.py`, `test_conformance_structural.py`'s renamed catalog-recheck tests, `test_null_cells.py`/`test_dangle_reference.py`/`test_mispoint_reference.py`'s pair-write tests, `test_insert_rows.py`'s minting tests, `test_selection.py`'s never-selectable tests, `test_base_writer.py`'s identity round-trip test) verified to test exactly what they claim.

### Gate 4: Test Value Audit

- **finding 1** (observation): `tests/corrupters/test_selection.py:734,752,764` — `test_family_a_mutability_predicate_excludes_identity_columns`, `test_nullability_predicate_excludes_identity_columns`, `test_jitter_eligibility_excludes_identity_columns` each hand-loop over the same 2-item `_IDENTITY_COLUMN_NAMES` tuple rather than using `pytest.mark.parametrize`, inconsistent with this same file's parametrize usage elsewhere. Not a blocker — each of the three tests exercises a genuinely different predicate (not 3 repeats of the same check), and 2 values is below the "≥3 near-identical bodies" multiplication threshold; a minor style nit only.

  No test-multiplication group (≥3 near-identical bodies differing only by literals) found among the sprint's additions that isn't already parametrized — the 5 new C5 negative fixtures and the taxonomy classification cases are both correctly expressed via `pytest.mark.parametrize`. No weak assertions (`len(x) > 0`, bare `is not None`, `a == b == c` chains) found in the sprint's new test bodies; new v6-specific tests consistently pin exact values (the `-1` dangling-sentinel value, exact minted `record_index` integers, the exact `{"name": "record_index", "type": "BIGINT"}` dict, exact taxonomy role strings) rather than existence checks. Spot-checked 5 of the ~45 mechanically-migrated fixture files with the largest diffs (`test_fk.py`, `test_lookup.py`, `test_history_tracked.py`, `test_conformance_data.py`, `test_relations.py`) for copy-paste column-order/type errors from the migration — none found; placeholder counts match value-list lengths and column-declaration order throughout.

### Gate 5: Coverage Analysis

Full run: `uv run pytest --cov=src/fabulexa_forge --cov-report=term-missing` — 3347 passed, 18 skipped, 0 failed, 97% overall. The one new source file this sprint creates, `src/fabulexa_forge/reader/records_columns.py`, is 100% covered; `exporters/source/plan.py` (the file with the largest behavioral change) is 100% covered; `corrupters/operations/null_cells.py` and `dangle_reference.py` are 100% covered.

- **finding 1** (observation): Three new "engine invariant" `CorruptError` branches added this sprint are uncovered:
  - `src/fabulexa_forge/corrupters/operations/mispoint_reference.py:188-191` and `:200-202` — `donor_record_index`'s two raises (`records__<kind>` absent from the working set; no row for the donor id).
  - `src/fabulexa_forge/corrupters/state.py:107-111` — `mint_record_index`'s raise (no captured high-water mark for the target table).

  All three are provably unreachable given their call sites as currently written: `donor_record_index` is only ever called after the eligibility loop's `if f"records__{target_kind}" not in state.tables: continue` filter (`mispoint_reference.py:436`) and only with a `donor_id` drawn from `resolve_donor_pool`'s own scan of that same table; `mint_record_index` is only called on records-category tables, which `CorruptState.__post_init__` always marks. This matches an existing pattern already present pre-sprint in `corrupters/operations/_impact.py` (e.g. lines 530/998/1003, also uncovered) — defensive guards against an engine invariant, documented as such in their own docstrings, left untested by house convention. Flagging per the skill's instruction to flag uncovered error paths "even if 'shouldn't happen'" — not a blocker.
- **finding 2** (observation): `src/fabulexa_forge/reader/conformance.py:537` and `:555` — in the amended `_check_c5_table`, the type-mismatch branch for the pinned prefix columns (line 537) and for `record_index`'s own type (line 555) are uncovered. This traces to a real gap in spec.md's own Phase 2 test list (see Gate 7c finding 2 below): the 5 named C5 negatives cover "`ref_index__` of a non-BIGINT type" but not "`record_index` itself of a non-BIGINT type" — a natural sixth case the spec didn't ask for and the implementer correspondingly didn't add.

### Gate 6: Type-ignore Density Check

Clean. `git diff <base>..HEAD -- 'tests/**/*.py' 'src/**/*.py' | grep "^+" | grep "type:\s*ignore"` returns exactly one line, `# type: ignore[index]` in a migrated fixture literal — one order of magnitude below the "review at >1 per file" heuristic, and nowhere near the "≥3 of the same shape" centralization trigger.

### Gate 7: Spec-Implementation Comparison

**7a — Sprint notes.** Read via `git notes --ref refs/notes/agent/sprint show <sha>` for all 4 phase commits (`ff1728a`, `51eb6b4`, `7de1c70`, `1627e29`). Notable recorded decisions, cross-checked against the code:
- Phase 1: `_records_columns` drops all identity-role columns except `record_id` (kept as `id`) — confirmed matches `plan.py:441`. A shared `_require_all_columns_classified` validator was added rather than duplicating classification per genre — confirmed single call site (`_default_columns`), guarding all non-junction genres uniformly (verified via `find_references`).
- Phase 2: 3 fixture files (`test_state_at.py`, `test_corrupt_source.py`, `test_engine.py`) were discovered missing from `state.yaml`'s 41-file enumeration mid-phase and fixed via the same migrate treatment — this matches the task brief's "41-file + 3-gap-file fixture migration" description and the final gate is green, so this is resolved, not outstanding.
- Phase 3: the implementer self-committed (`7de1c70`) before the orchestrator's normal commit step — a process deviation, but the note records it was verified via `git show --stat` before proceeding and content matched what was reviewed. No content-level concern found in this review either.
- Phase 4: `CorruptState.__post_init__` captures the high-water mark eagerly (not lazily) specifically so a same-run `delete_rows` suffix-removal before `insert_rows` can't shrink the mark — confirmed against `state.py` and the Phase 4 demo, which explicitly exercises and prints this case (`minted phantom indices [5, 6]` strictly above the pre-delete maximum `4`).

**7b — Spec → impl (contracts).** Compared every "Contracts" and "Modified behavior" entry in `spec.md` against its implementation: `RecordsColumnRole`/`records_column_role`/`ref_index_sibling`/`REF_INDEX_PREFIX` (records_columns.py), `identity_column` (sidecar_builder.py), `SourceUnclassifiedColumn` (errors.py), the amended `_check_c5_table`/`_check_c5_property_block` (conformance.py), `write_emit`'s shape assertion, the source plan resolvers' identity-drop posture, `init`'s role-scoped proposal loop, the 3 corrupters' pair-scoped writes, `insert_rows`'s high-water-mark minting, and `is_jitter_eligible`'s reference-exclusion clause. All match their docstrings; all 4 phase demos (except Phase 1, see Gate 10) independently confirm the described behavior end-to-end against real output. No blockers in this direction.

**7c — Impl → spec (auditing the spec itself).**
- **finding 1** (observation): spec.md's Success Criterion #1 — "`fabulexa-forge validate docs/examples/parent-child/published` passes C1–C13" — cannot be executed as literally stated in any fresh git checkout. `docs/examples/parent-child/published/` in this worktree contains only `ATLAS.md` and `base.json` (verified via `ls`); `run.duckdb` is deliberately excluded from git per repo policy (confirmed in the Phase 2 sprint note: "that example's run.duckdb is deliberately never committed... so the demo as originally spec'd could never succeed in any git checkout"). The `base.json` itself *is* genuinely v6-shaped (spot-checked: `records__actor` carries `record_index` at its pinned slot and `ref_index__group` immediately after `prop__group`), so the criterion's intent is satisfied — but the literal command in the spec can never be run to confirm it. The implementer correctly worked around this for the Phase 2 demo (which builds its v6 emit inline instead); the spec's stated criterion was never corrected to match. Process note for spec-writing: a success criterion should not name a path whose required artifact is excluded from version control.
- **finding 2** (observation): spec.md's Phase 2 test list (5 new C5 negatives) is missing a natural 6th case symmetric with the ones it does list: "`record_index` itself declared a non-BIGINT type" (distinct from the listed "`ref_index__` of a non-BIGINT type"). This is the direct cause of Gate 5 finding 2's uncovered branch (`conformance.py:555`) — the implementation faithfully covers exactly what the spec asked for; the spec asked for one fewer case than the check itself actually guards.

### Gate 8: Workspace Check

Clean. `git status --porcelain` returns no output — no untracked files, nothing uncommitted.

### Gate 9: Lint & Typecheck

Clean. `make lint`: `ruff check .` → "All checks passed!"; `ruff format --check .` → "249 files already formatted". `make typecheck`: `uv run mypy src` → "Success: no issues found in 92 source files".

### Gate 10: Demo Verification

- **finding 1 (BLOCKER)**: `docs/sprints/base-format-v6/demos/phase_1_taxonomy_posture.py` crashes on both runs (exit code 1, identical traceback both times):
  ```
  fabulexa_forge.reader.errors.UnsupportedBaseFormatVersionError: unsupported base_format_version 5; no auto-upgrade
  ```
  raised from `Sidecar.from_raw` inside `_print_source_plan_posture` (demo line 138: `Sidecar.from_raw(_build_v5_sidecar_raw(_WIDGET_COLUMNS))`). The demo hand-builds a `base_format_version: 5` sidecar on purpose — its own docstring says so ("Builds a small v5 emit inline... the demo stays green while the vendored contract is already pinned to v6, ahead of the Phase-2 flip"), which was true when Phase 1 was implemented and reviewed (`SUPPORTED_BASE_FORMAT_VERSION` was still 5 at that point, per the Phase 1 sprint note's gate-waiver text). Phase 2 then flipped `SUPPORTED_BASE_FORMAT_VERSION` to 6 globally (as the atomic flip it was designed to be), which makes `Sidecar.from_raw` refuse any `base_format_version: 5` sidecar — including this demo's own inline fixture. Nothing in Phase 2/3/4 touched or re-verified the Phase 1 demo, so this regression was never caught until this review. The first section of the demo (`_print_taxonomy_table`, pure name classification with no sidecar) does run and print correctly before the crash. This must be fixed before merge — either rebuild the demo's inline sidecar at `base_format_version: SUPPORTED_BASE_FORMAT_VERSION` (mirroring the Phase 2/3/4 demos' own inline-v6-fixture pattern) or otherwise adjust it to exercise the taxonomy/posture behavior without asserting a v5-labeled emit through `Sidecar.from_raw`.

  Phases 2, 3, and 4 demos all pass cleanly on both runs and produce byte-identical output across runs (verified via `diff`). Spot-checked their output content, not just exit codes: Phase 2's demo shows all of C1–C13 passing on an inline v6 emit and `record_index`/`ref_index__group` at their pinned column slots; Phase 3's demo shows the three pair-write operations producing exactly one `DefectRecord` each with the documented `(prop__actor, ref_index__actor)` pair shapes (`('__dangling__0', -1)`, `(None, None)`, donor co-point); Phase 4's demo shows minted phantom indices `[5, 6]` strictly above a pre-delete maximum of `4`, and a reference pair traveling unchanged through `duplicate_rows` jitter. All three match their spec.md demo descriptions and their phase's test-list claims.

## Recommendation

**REVISIONS NEEDED** — one blocker (Gate 10: `phase_1_taxonomy_posture.py` demo crashes deterministically post-Phase-2-flip). All other gates are clean or observation-only; none of the 9 recorded observations require blocking the merge on their own. Fix the Phase 1 demo (the fix is narrowly scoped — the demo's inline fixture needs to stop asserting a v5-labeled sidecar now that the flip is final) and re-run Gate 10 before merge. The observations are recorded for the user's own per-item accept/fix decision and are not gating.
