# Sprint Review: slice-only-policy

Date: 2026-07-19
Reviewer: Fresh-eyes sprint reviewer (Claude Fable 5)

## Summary table

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code scan | clean | 0 | No `# Future:`/`# TODO`, no pass-only bodies in sprint diff; every sprint-added public symbol (`Notice`, `NoticeSink`, `render_notice_stderr`, `is_exempt_discriminator`, `is_non_exempt_slice_only`, `slice_only_refusal_message`, `SourceRenameSliceOnly`, `_check_stream_properties_slice_only`, `_omitted_slice_only_columns`, `_apply_rename_entry`'s new params) traced via `find_references` to a production caller outside tests/demos. |
| 2. Consistency / DRY | observations | 1 | `_unit_label` (plan.py) has a single call site; minor, not a violation — noted for completeness. |
| 3. Test name audit | clean | 0 | Sampled test_slice_only.py, test_notices.py, test_validation.py, test_fk.py, test_lookup.py, test_plan.py, test_renders.py, streaming test_engine.py, test_cli_init.py — every body matches its name/docstring claim (ordering, determinism, error type/message). |
| 4. Test value audit | observations | 1 | The four `derived: elapsed` sub-surface tests (`correlate_on`/`start_source`/`end_source`/`other_where`) in test_validation.py are structurally identical modulo the varied kwarg — a parametrize candidate. Assertions throughout are precise (exact message/content/row-shape pins), not weak isinstance/None checks. |
| 5. Coverage | observations | 1 | Overall 97%, all new modules (`notices.py`, `slice_only.py`) 100%. One new-sprint line is uncovered: `plan.py:720`, the split-unit (`sub_type is not None`) branch of `_unit_label` — no slice-only-omission test exercises a sub-typed unit, so the "names the unit (source table + sub_type)" notice-message contract is unverified for split units. |
| 6. Type-ignore density | clean | 0 | Exactly one `# type: ignore[misc]` added across the whole test diff (test_notices.py:168, a frozen-dataclass mutation-raises assertion) — well under the 1-per-file / 3-same-shape threshold. |
| 7. Spec ↔ codebase | blockers | 1 | Phase 1 demo (`demos/phase_1_notice_channel.py`) deterministically fails: its inline `_RECORDS_COLUMNS` fixture (lines 43-44) declares `history_tracked: False` on `prop__name`/`prop__entity_type` with no paired `temporal_class`, which C13-fails under the reader's conformance check, so `cmd_export` exits 1 instead of the demo's expected 0. See Gate 10. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty; no untracked files. |
| 9. Lint & typecheck | clean | 0 | `make lint` (ruff check + ruff format --check) and `make typecheck` (mypy --strict) both pass with 0 issues. `pre-commit run --files <changed files>` (independent check) also passes all hooks. |
| 10. Demos | blockers | 1 | Phase 1 demo exits 1 on both runs (deterministic failure, not a flake) — see Gate 7 above. Phases 2, 3, 4 each ran twice with byte-identical stdout/stderr and exit 0. |

## Findings

### Gate 2 — Consistency / DRY

1. `src/fabulexa_forge/exporters/source/plan.py:710` (`_unit_label`) — single call site (`_slice_only_omission_notice` at plan.py:737).
   Severity: observation. Not a violation of any principle — it is a small, well-named decomposition for a docstring-declared contract ("naming the unit (source table + sub_type)"), independently testable. Noted only because a lone-caller helper is a candidate the DRY gate is tuned to surface; no action required.

### Gate 4 — Test value audit

1. `tests/exporters/dimensional/test_validation.py` — `test_derived_elapsed_correlate_on_refuses_slice_only`, `test_derived_elapsed_start_source_refuses_slice_only`, `test_derived_elapsed_end_source_refuses_slice_only`, `test_derived_elapsed_other_where_key_refuses_slice_only` (approx. lines 340-380 per the diff).
   Severity: observation. Four tests share an identical body shape — build `_elapsed_col(<field>=...)`, build a `TableDecl`, assert `check_slice_only_column_reads` raises with the shared message — differing only in which `_elapsed_col` kwarg carries the slice_only source. A `@pytest.mark.parametrize` over `(correlate_on, start_source, end_source, other_where)`-shaped inputs would collapse these to one parametrized test without losing per-surface coverage. Not a Principle #7 issue; purely a maintainability note. (The `from`/`correlation`/`value_map.from` trio nearby is *not* flagged — those differ in which `ColumnDecl` constructor argument is set, not just a literal, so they are legitimately separate tests.)

### Gate 5 — Coverage

1. `src/fabulexa_forge/exporters/source/plan.py:720` — `_unit_label`'s `unit.sub_type is not None` branch is never executed by the suite.
   Severity: observation. Every `slice-only-column-omitted` notice test in `tests/exporters/source/test_plan.py` (`test_notice_emitted_once_per_omitted_column`, `test_notice_order_unit_order_then_sidecar_column_order`, `test_degenerate_unit_every_property_slice_only_still_renders`, etc.) uses an unsplit kind (`patient`, `visit`, `order`, `member`). None exercises a sub-typed/split unit carrying a slice_only column, so the documented "`unit '<source_table> (sub_type '<sub_type>')'`" notice-message shape for split units is unverified. `build_source_plan`'s docstring and `_resolve_specs`'s contract both promise this naming; the coverage gap means a regression in the split-unit branch would not be caught.

### Gate 7 / Gate 10 — Spec ↔ codebase / Demos

1. `docs/sprints/slice-only-policy/demos/phase_1_notice_channel.py:43-44` — the demo's inline `_RECORDS_COLUMNS` fixture:
   ```python
   {"name": "prop__name", "type": "VARCHAR", "history_tracked": False},
   {"name": "prop__entity_type", "type": "VARCHAR", "history_tracked": False},
   ```
   declares `history_tracked` with no paired `temporal_class`. Every other fixture touched by this sprint (test fixtures across all four phases) was updated to pair `history_tracked` with `temporal_class` — this demo fixture was missed. Running it fails deterministically (verified twice, identical output both times):
   ```
   notice: discriminator value 'admin' not observed for 'entity.prop__entity_type'; table will be empty
   ERROR: records__entity.prop__name declares history_tracked but no temporal_class; the emit is non-conformant (C13). Run `fabulexa-forge validate`.
   FAIL: expected exit 0, got 1
   ```
   Severity: blocker. The spec's Phase 1 "Demo" line and the Success Criteria's `make check` green bar don't explicitly gate demo execution, but the sprint's own `state.yaml` lists `demo: "docs/sprints/slice-only-policy/demos/phase_1_notice_channel.py"` as a phase-1 deliverable, and the review protocol's Gate 10 requires the four demos to run clean. This one does not — it is broken by a one-line fixture omission (add `"temporal_class": "constant"` to both columns), not a design flaw, but it must be fixed before sign-off.

## Recommendation

REVISIONS NEEDED — 1 blocker (Phase 1 demo fails deterministically; fix the fixture's missing `temporal_class` pairing), 3 observations (a lone-caller helper, a parametrize candidate in the elapsed sub-tests, and an uncovered split-unit notice-naming branch). No Principle #7 violations found; contract compliance (signatures, docstrings, raises) matches the spec exactly across every reviewed module; lint, typecheck, pre-commit, and the full test suite (3419 passed, 18 skipped) are all green.
