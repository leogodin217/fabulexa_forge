# Sprint Review: slice-only-policy

Date: 2026-07-19
Reviewer: Fresh-eyes sprint reviewer (Claude Fable 5)

## Summary table

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code scan | clean | 0 | No `# Future:`/`# TODO`, no pass-only bodies in sprint diff; every sprint-added public symbol (`Notice`, `NoticeSink`, `render_notice_stderr`, `is_exempt_discriminator`, `is_non_exempt_slice_only`, `slice_only_refusal_message`, `SourceRenameSliceOnly`, `_check_stream_properties_slice_only`, `_omitted_slice_only_columns`, `_apply_rename_entry`'s new params) traced via `find_references` to a production caller outside tests/demos. |
| 2. Consistency / DRY | observations | 1 | `_unit_label` (plan.py) has a single call site; minor, not a violation — noted for completeness. |
| 3. Test name audit | clean | 0 | Sampled test_slice_only.py, test_notices.py, test_validation.py, test_fk.py, test_lookup.py, test_plan.py, test_renders.py, streaming test_engine.py, test_cli_init.py — every body matches its name/docstring claim (ordering, determinism, error type/message). |
| 4. Test value audit | clean | 0 | Resolved at user's direction: the four `derived: elapsed` sub-surface tests collapsed into one `@pytest.mark.parametrize`d test with 4 ids. Assertions throughout are precise (exact message/content/row-shape pins), not weak isinstance/None checks. |
| 5. Coverage | clean | 0 | Resolved at user's direction: `test_split_unit_omission_notice_names_source_table_and_sub_type` (test_plan.py) now exercises the split-unit branch; `plan.py:720` covered (plan.py 99%, only pre-existing line 388 uncovered — outside sprint diff). |
| 6. Type-ignore density | clean | 0 | Exactly one `# type: ignore[misc]` added across the whole test diff (test_notices.py:168, a frozen-dataclass mutation-raises assertion) — well under the 1-per-file / 3-same-shape threshold. |
| 7. Spec ↔ codebase | clean | 0 | Prior blocker (Phase 1 demo fixture missing `temporal_class` pairing) resolved by commit `0255382` ("Sprint slice-only-policy - review cleanup"), which adds `"temporal_class": "constant"` to both `prop__name` and `prop__entity_type` in `demos/phase_1_notice_channel.py`. Re-verified: no other findings. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty; no untracked files. |
| 9. Lint & typecheck | clean | 0 | `make lint` (ruff check + ruff format --check) and `make typecheck` (mypy --strict) both pass with 0 issues. `pre-commit run --files <changed files>` (independent check) also passes all hooks. |
| 10. Demos | clean | 0 | Resolved (commit `0255382`). All four demos re-run twice each: `phase_1_notice_channel.py`, `phase_2_dimensional_refusal.py`, `phase_3_source_omission.py`, `phase_4_streaming_refusal.py` — every run exits 0, and each phase's two runs produced byte-identical stdout+stderr. |

## Findings

### Gate 2 — Consistency / DRY

1. `src/fabulexa_forge/exporters/source/plan.py:710` (`_unit_label`) — single call site (`_slice_only_omission_notice` at plan.py:737).
   Severity: observation. Not a violation of any principle — it is a small, well-named decomposition for a docstring-declared contract ("naming the unit (source table + sub_type)"), independently testable. Noted only because a lone-caller helper is a candidate the DRY gate is tuned to surface; no action required.

### Gate 4 — Test value audit — RESOLVED

1. `tests/exporters/dimensional/test_validation.py` — `test_derived_elapsed_correlate_on_refuses_slice_only`, `test_derived_elapsed_start_source_refuses_slice_only`, `test_derived_elapsed_end_source_refuses_slice_only`, `test_derived_elapsed_other_where_key_refuses_slice_only` (approx. lines 340-380 per the diff).
   **Resolved** at the user's FIX direction: collapsed into one `@pytest.mark.parametrize`d `test_derived_elapsed_refuses_slice_only` with ids `correlate_on`/`start_source`/`end_source`/`other_where_key`; per-surface coverage retained. The `from`/`correlation`/`value_map.from` trio untouched, as prescribed.
   Severity: resolved (was observation). Four tests share an identical body shape — build `_elapsed_col(<field>=...)`, build a `TableDecl`, assert `check_slice_only_column_reads` raises with the shared message — differing only in which `_elapsed_col` kwarg carries the slice_only source. A `@pytest.mark.parametrize` over `(correlate_on, start_source, end_source, other_where)`-shaped inputs would collapse these to one parametrized test without losing per-surface coverage. Not a Principle #7 issue; purely a maintainability note. (The `from`/`correlation`/`value_map.from` trio nearby is *not* flagged — those differ in which `ColumnDecl` constructor argument is set, not just a literal, so they are legitimately separate tests.)

### Gate 5 — Coverage — RESOLVED

1. `src/fabulexa_forge/exporters/source/plan.py:720` — `_unit_label`'s `unit.sub_type is not None` branch is never executed by the suite.
   **Resolved** at the user's FIX direction: added `test_split_unit_omission_notice_names_source_table_and_sub_type` in `tests/exporters/source/test_plan.py`, building a split (sub-typed) unit carrying a non-exempt slice_only column and asserting the exact `unit '<source_table> (sub_type '<sub_type>')'` notice naming for both split units. `plan.py:720` now covered.
   Severity: resolved (was observation). Every `slice-only-column-omitted` notice test in `tests/exporters/source/test_plan.py` (`test_notice_emitted_once_per_omitted_column`, `test_notice_order_unit_order_then_sidecar_column_order`, `test_degenerate_unit_every_property_slice_only_still_renders`, etc.) uses an unsplit kind (`patient`, `visit`, `order`, `member`). None exercises a sub-typed/split unit carrying a slice_only column, so the documented "`unit '<source_table> (sub_type '<sub_type>')'`" notice-message shape for split units is unverified. `build_source_plan`'s docstring and `_resolve_specs`'s contract both promise this naming; the coverage gap means a regression in the split-unit branch would not be caught.

### Gate 7 / Gate 10 — Spec ↔ codebase / Demos — RESOLVED

1. `docs/sprints/slice-only-policy/demos/phase_1_notice_channel.py:43-54` — **Resolved** by commit `0255382` ("Sprint slice-only-policy - review cleanup"), which pairs `"temporal_class": "constant"` onto both `prop__name` and `prop__entity_type` in the demo's inline `_RECORDS_COLUMNS` fixture, matching the pairing pattern already used by every other fixture in this sprint's diff.
   Re-verification (this pass): `git show HEAD` confirms the fix touches only the demo fixture (+`review.md`) — no production code changed. All four demos (`phase_1_notice_channel.py`, `phase_2_dimensional_refusal.py`, `phase_3_source_omission.py`, `phase_4_streaming_refusal.py`) were re-run twice each: every run exits 0, and each phase's two runs are byte-identical on stdout+stderr. `git status --porcelain` is empty (Gate 8) and `make lint typecheck` is clean (Gate 9; ruff check, ruff format --check, mypy --strict all pass). The fix is a pure two-line dict-literal addition to demo/test fixture data (not scenario-author config), so it introduces no Principle #7 concern and no new smell.
   Severity: resolved (was blocker).

## Recommendation

APPROVED-WITH-NOTES — 0 blockers (the prior Phase 1 demo blocker is resolved by commit `0255382`). Of the 3 observations, two were fixed at the user's FIX direction (elapsed sub-tests parametrized; split-unit notice-naming branch now covered) and one was explicitly accepted by the user with no action (lone-caller `_unit_label` helper, Gate 2). No Principle #7 violations found; contract compliance (signatures, docstrings, raises) matches the spec exactly across every reviewed module; lint, typecheck, pre-commit, all four demos (re-run twice each, byte-identical, exit 0), and the full test suite (3420 passed, 18 skipped) are all green.
