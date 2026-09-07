# Sprint Review: shaped-table-selection

**Date:** 2026-09-07
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# TODO`/`# Future:`/bare-`pass` scaffolding in the diff; no inert self-renames; all 4 sprint-added public symbols (`_selected_table_decls`, `check_reserved_table_name`, `check_key_columns_stable`, `_resolve_selection`) verified via `find_references` to have production callers, not just tests. |
| 2. Consistency / DRY | clean | 0 | Tier-2 loaded (all sibling files in `exporters/dimensional/`, `exporters/source/`, `playback/`, `incremental/`). `playback/selection.py`'s `resolve_selection` is a distinct tier-1 record/membership-property concern, not a duplicate of the new tier-2 table-name `_resolve_selection`. No pre-existing "select declared items by name set, decl order" helper found anywhere in the workspace (`find_workspace_symbols` for `selected`/`_from_col` surveyed). Per-file `_from_col` micro-helpers match the codebase's existing established convention (8+ pre-existing test files already do this). |
| 3. Test names | clean | 0 | Spot-checked `test_selection.py`, `test_shaped_selection.py`, `test_windowing.py`, `test_validation.py`, `test_engine.py` — names match bodies; `test_snapshot_table_unstable_key_refused` correctly replaces the flipped `test_snapshot_table_key_not_checked`; ordering test names ("dim-side leg guard runs/skipped") match their assertions. |
| 4. Test value | observations | 2 | See Findings. |
| 5. Coverage | clean | 0 | `engine.py` (dimensional) 99%, `engine.py` (source) 99%, `validation.py` 98%, `windowing.py` 90%, `reserved_names.py` 100%, `driver.py` 99%, `playback/shaped.py` 99%. Every sprint-added line (`check_key_columns_stable`, `check_reserved_table_name`, `_selected_table_decls`, `_resolve_selection`, the selection/horizon-economy branches in both engines) is covered; all uncovered lines pre-exist the sprint and sit outside its diff (the horizon-invariance reading's untouched branches, an unrelated `ValueError` guard, an unrelated CSV-staging path, one `unreachable:` defensive `AssertionError`). 5823+2403 tests passed, 0 failed. |
| 6. Type-ignore density | clean | 0 | Zero new `# type: ignore` markers added by the diff. |
| 7. Spec ↔ codebase | clean | 0 | 7a: sprint notes read for all 3 phase commits, decisions consistent with code (e.g. `_selected_table_decls` extraction, lazy `start_specs`, post-hoc source-selection filter). 7b: `check_key_columns_stable`, `check_reserved_table_name`, `validate_table`, `_build_windowed_query_specs`, `build_query_specs`, `build_windowed_source_query_specs`, `_resolve_selection`, `_compile_window_specs`/`_compile_state_specs`, `ShapedPlayback.window`/`.state` all diffed line-for-line against spec's prescribed signatures/docstrings/behavior — no divergence found. 7c: no new helper/constant duplicates an existing one; the spec's placement decision (stable-key rule in `windowing.py` to avoid a circular import) is itself a documented resolution of a real layering constraint, not a miss. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty. |
| 9. Pre-commit | clean | 0 | `pre-commit run --all-files` — all 8 hooks passed (ruff, ruff format, mypy strict, understand bundles, etc.), no auto-fixes applied. |
| 10. Demos | clean | 0 | All three phase demos (`phase_1_always_on_key_rules.py`, `phase_2_dimensional_selection.py`, `phase_3_shaped_table_selection.py`) ran successfully twice with byte-identical output both runs; exit code 0 each time. |

## Findings

### Gate 4: Test Value

- **finding 1** (observation): `tests/playback/test_shaped_selection.py:400-419` —
  `test_state_delivery_is_snapshot_on_every_selected_table_dimensional` and
  `_source` each ask for exactly 2 named tables (`tables={"dim_gadget",
  "fact_shipment"}` / `tables={"widget_parts", "widget_versions"}`) but assert
  only `assert tables` (a `len(x) > 0`-shaped truthy check) before `assert
  all(t.delivery == "snapshot" for t in tables)`. The exact selected count (2)
  is known from the call site and is never pinned — a regression that answered
  3 tables (or 1) with 'snapshot' delivery would still pass. Fix: assert
  `len(tables) == 2` or `{t.name for t in tables} == {"dim_gadget",
  "fact_shipment"}` explicitly.
- **finding 2** (observation): `tests/playback/test_shaped_selection.py:95-132` —
  four tests (`test_window_tables_bare_str_raises_playback_error_dimensional`,
  `_source`, `test_state_tables_bare_str_raises_playback_error_dimensional`,
  `_source`) differ only in the axis (window/state) and mode
  (dimensional/source) crossed against an otherwise identical body (open a
  head, call the method with `tables="booking"`, expect `PlaybackError`
  matching `"not a str"`, assert `"booking"` in the message). A reasonable
  parametrization candidate
  (`@pytest.mark.parametrize("method,mode", ...)`), though each does cover a
  genuine 2x2 matrix cell rather than repeating the same case with only a
  literal changed, so this is noted rather than blocking.

## Recommendation

**APPROVED-WITH-NOTES** — no blockers; two observations recorded (both in
Gate 4, both minor test-quality notes on `tests/playback/test_shaped_selection.py`).
Mergeable as-is; fix-vs-accept on the two observations is the user's call at
the ACCEPT/FIX checkpoint.


## Post-review fixes (user-directed FIX)

- Observation 1 (weak `assert tables` in the two state-delivery tests): FIXED — now pins the exact selected table names.
- Observation 2 (four bare-str gate tests): FIXED — collapsed into one parametrized test over window/state × dimensional/source.
- Gate after fixes: 5823 passed, 23 skipped, 0 failed. Pre-commit: PASS.
