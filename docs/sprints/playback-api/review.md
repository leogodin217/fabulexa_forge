# Sprint Review: playback-api

Date: 2026-07-20

Reviewer: Fresh-eyes sprint reviewer (Claude, /review-sprint)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code scan | clean | 0 | No `# Future:`/`# TODO`/bare `pass` scaffolding in sprint diff; every new public symbol (`open_playback`, `Playback`, `open_shaped_playback`, `ShapedPlayback`, atom/selection types, `PlaybackEvent`, `PlaybackSnapshot`/`PlaybackPosition`) is exported per the spec's declared public surface and has production/demo callers; `apply_base_relations` (internal, not re-exported) is called from both engine modules. |
| 2. Consistency/DRY | observations | 1 | `ShapedPlayback.state()`'s `_truncated_emit_view` reaches into `Emit._conn`, a private attribute of a sibling module's class, rather than a public accessor — see Findings. All other extractions (`query_spec_output_name`, `spine_discriminator_index`, `build_history_asof_join`, `render_ts`, `apply_base_relations`) are genuinely shared, single-source-of-truth helpers per the sprint notes' stated intent, confirmed via `find_references`. |
| 3. Test name audit | clean | 0 | Spot-checked `test_events.py`, `test_consistency.py`, `test_base_relations.py` — every test body matches its name's claim (order, seq-invariance, binding-rule, algebra tests all assert what they say). |
| 4. Test value audit | clean | 0 | `test_consistency.py`'s 7-case algebra test is already `@pytest.mark.parametrize`d (not a multiplication candidate); assertions throughout are structural equality on dicts/tuples, not weak `len>0`/`is not None` checks. |
| 5. Coverage | observations | 1 | Full suite: 3636 passed, 18 skipped, 97% total. All new sprint files ≥ 92%. `playback/events.py` (92%) has real uncovered branches: `presentation_id` stamping (both record after-image sites) and membership `owner_sub_types`/`owner_record_ids` population restriction are never exercised by a fixture that carries them — see Findings. |
| 6. Type-ignore density | observations | 1 | `tests/derivations/test_truncated_tape.py` adds 2 new `# type: ignore[union-attr]` (same shape) in one test — over the ">1 per file" threshold; pre-existing ignores in other touched test files are untouched by this diff. |
| 7. Spec ↔ codebase | clean | 0 | Sprint notes read for all 12 phases (0 open review cycles except Phase 1 and Phase 8, both resolved same-commit). Spot-checked contracts (`build_membership_state_at_sql`, `build_state_at_end_sql`, the three truncated-tape builders, `resolve_selection`/`ResolvedSelection`, `PlaybackEvent`, `open_playback`/`Playback`, `open_shaped_playback`/`ShapedPlayback`, `build_query_specs`/`build_source_query_specs`+`base_relations`, `shadow_base_relations`, the two declared mode changes) against the design doc's Interface Contracts section and the spec's deltas — signatures, docstrings, and raises all match verbatim. `SourceSnapshotRequiresWindows` fully deleted (no references anywhere). |
| 8. Workspace | clean | 0 | `git status --porcelain` empty before and after running the full suite and every demo twice. |
| 9. Lint & typecheck | clean | 0 | `make lint`: ruff check + format both pass. `make typecheck`: mypy strict, 107 source files, 0 issues. |
| 10. Demos | clean | 0 | All 12 `docs/sprints/playback-api/demos/phase_*.py` ran twice; exit 0 both times; byte-identical output across both runs (diff empty for all 12). |

## Findings

### Gate 2: Consistency/DRY

1. **`src/fabulexa_forge/playback/shaped.py:303`** — `_truncated_emit_view` reads `emit._conn` to compose the truncated `Emit` view, reaching past `reader/emit.py`'s public surface (`Emit` exposes only `sidecar`, `emit_dir`, `query`, `query_arrow`, `close` — no `conn` property). Severity: observation. Not a correctness bug (it's tested — `tests/playback/test_shaped_state.py::test_truncated_emit_view_never_closes_the_callers_connection` — and `ruff`'s enabled rule set (`E`,`F`,`I`) doesn't flag private-attribute access), but it's a layering smell: the one sanctioned reader composition point (`Emit(sidecar=..., emit_dir=..., conn=...)`, per the design doc's own § Shaped state wording) has no matching public way to *extract* `conn` from an already-open `Emit`, so tier-2's only route in is the private attribute. A `conn` property on `Emit` would have closed this gap without changing behavior.

### Gate 5: Coverage

1. **`src/fabulexa_forge/playback/events.py:137-139, 204-206`** (presentation_id stamping in both `_build_record_after_image` and `_build_record_event_rows`) and **`:245, 247`** (`_membership_field_name`'s `member__<f>__kind` / `member__<f>__id` reference-field recovery) and **`:337, 342`** (membership `owner_sub_types_filter` / `owner_record_ids_filter` row restriction in `_build_membership_event_rows`) are uncovered. Severity: observation. `tests/playback/test_events.py`'s fixtures (`_PATIENT_COLS`, `_WIDGET_COLS`, `_TEAM_COLS`, `_TAGS_COLS`) never declare a `presentation_id` column, never declare a reference-typed membership field (`member__<f>__kind`/`__id` pair — only scalar `elem__` fields), and `TestPopulationRestriction` only exercises `sub_types`/`record_ids` on the record family, never `owner_sub_types`/`owner_record_ids` on the membership family — even though the spec's Phase 6 test list calls out "population axes ... change the in-scope stream" and the doc's `PlaybackEvent.presentation_id` docstring is exercised nowhere in this file. These are real, not merely incidental, gaps: a caller selecting a presentation-id-bearing kind, a reference-typed membership field, or restricting a membership atom's owner population is untested code.

### Gate 6: Type-ignore density

1. **`tests/derivations/test_truncated_tape.py:411-412`** — two `# type: ignore[union-attr]` of the same shape (`truncated.record_roles().kinds()` / `emit.sidecar.record_roles().kinds()`, both narrowed by a preceding `x is not None` in the same `and`-chain that mypy doesn't propagate across). Severity: observation, per the gate's own remediation guidance ("remediation is a test helper, never a production typing change") — e.g. binding `record_roles()` to a local variable and asserting non-None on it before the comparison would avoid both ignores without touching production code.

## Recommendation

APPROVED-WITH-NOTES
