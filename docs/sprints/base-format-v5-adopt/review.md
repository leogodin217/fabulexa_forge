# Sprint Review: base-format-v5-adopt

**Date:** 2026-07-15
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# Future:`/`# TODO`, no bare-`pass` bodies, no inert self-renames in the diff; every sprint-added `src/` symbol (`TemporalClass`, `ColumnNotFoundError`, `TemporalClassUnavailableError`, `_check_c13*`, `_check_c11_converse`, `_any_records_prop_history_tracked`, `history_pair_row_count`, `c11_converse_broken`) traces to a production caller (registry, `_check_c11`/`_check_c13`, `drop_events.py`, `plan.py`), not just tests. |
| 2. Consistency / DRY | observations | 1 | See Gate 2 finding — a small filter-block duplication between `_check_c11_converse` and `_check_c13`'s inline loop. |
| 3. Test names | clean | 0 | Spot-checked `test_drop_entire_series_with_records_row_declares_c11`, `test_history_tracked_without_temporal_class_raises_citing_c13`, all `sidecar_builder` tests, and the C13 structural/semantic cases — each body verifies exactly what its name claims, with exact-value assertions (`assert {d.impact for d in outcome.defects} == {("C11",)}`, message-substring checks naming C13/values). |
| 4. Test value | clean | 0 | No test-name group of ≥3 near-identical bodies in the diff (all new test names unique, deliberate `@pytest.mark.parametrize` used where three enum values are exercised); no `len(x) > 0` / bare `is not None` weak assertions found in the diffed test bodies. |
| 5. Coverage | observations | 1 | 97% total, no regression. New code is fully covered except one shared branch — see Gate 5 finding. |
| 6. Type-ignore density | observations | 1 | 3 `# type: ignore[arg-type]` added in one file (`tests/reader/_emit_helpers.py`), same shape, above the >1-per-file heuristic. |
| 7. Spec ↔ codebase | clean | 0 | Both directions checked; see Gate 7 notes. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty — no stray untracked files. |
| 9. Lint & typecheck | clean | 0 | `ruff check`, `ruff format --check`, `mypy src` all pass with zero issues. |
| 10. Demos | clean | 0 | All 6 `phase_*.py` demos ran twice; exit 0 both times; byte-identical output both runs (diffed). |

## Findings

### Gate 2: Consistency / DRY

- **finding 1** (observation): `src/fabulexa_forge/reader/conformance.py` — `_check_c11_converse` (lines ~1433-1446) and `_check_c13`'s inline records/prop__ loop (lines ~1758-1780) both filter records-category `prop__` columns with the identical four-step predicate (`name.startswith("prop__")` → `history_tracked is True` → `type.upper().strip() in _ROUND_TRIPPABLE_TYPES` → strip the `prop__` prefix) before doing check-specific work (append a converse-violation message vs. call `_check_c13_genesis`). The shared constant (`_ROUND_TRIPPABLE_TYPES`) is correctly reused rather than reinvented (no P7/DRY violation there), but the filter block itself is duplicated verbatim across the two functions. A small shared generator (e.g. `_flagged_roundtrippable_props(table_spec) -> Iterator[tuple[str, ColumnSpec]]`) would remove the duplication. Not a blocker — the duplication is four lines, entirely mechanical, and each call site's post-filter action differs — but it is a legitimate consolidation candidate for a future pass.

### Gate 5: Coverage

- **finding 1** (observation): `src/fabulexa_forge/reader/conformance.py` line 1779 (the round-trippable-type exclusion `continue` inside `_check_c13`'s genesis-candidate loop) is uncovered. The C13 docstring explicitly claims this exclusion ("Collection-struct properties stay outside the semantic clause's input set — excluded by the same round-trippable-type gate shipped C6 uses"), and the C11-converse side of the identical gate *is* tested (`test_c11_converse_gate_excludes_non_round_trippable_column`), but no equivalent test constructs a C13 scenario with a non-round-trippable flagged column to confirm the genesis check skips it too. Two sibling defensive-skip branches (`_check_c11_converse`'s `kind is None` guard and its catalog-absent skip, lines 1420/1424-1428) are also uncovered, but these mirror the same historically-uncovered skip-guard pattern already present pre-sprint in C6/C12 (e.g. lines 1136-1139, 1212-1216), so they are not a sprint-introduced regression in rigor — only the C13/round-trippable gap is a genuine, spec-documented behavior left unverified.

### Gate 6: Type-ignore density

- **finding 1** (observation): `tests/reader/_emit_helpers.py` gained 3 `# type: ignore[arg-type]` comments (on `tables=sidecar["tables"]`, `branches=sidecar.get("branches")`, `base_format_version=sidecar.get("base_format_version")`), all routing an untyped `sidecar: dict[str, object]` through `_support.sidecar_builder.write_emit`'s typed keyword parameters. Same shape, same file, count 3 — above the `>1 per file` / `>=3 of the same shape` heuristic. The remediation (per the heuristic) is a small typed accessor/cast helper local to this adapter function, not a change to `write_emit`'s production signature. Not a blocker; the underlying design (decompose an already-existing untyped dict and route it through the new typed writer) is sound and this is the one call site that needed it.

## Gate 7 detail (spec ↔ codebase, both directions)

**7a — sprint notes.** Read via `git notes --ref refs/notes/agent/sprint` for all 6 phase commits plus the plan commit. Both mid-sprint deviations named in the task were traced and verified clean:
  - Phase 3's `tests/corrupters/_helpers.py` `column_spec` pairing-enforcement scope expansion (6 unlisted `corrupters/operations` test files) is fully reflected in `state.yaml`'s phase-3 second `migrate` step and in the diffed files; all 6 files (`test_dangle_reference.py`, `test_delete_rows.py`, `test_duplicate_rows.py`, `test_mispoint_reference.py`, `test_null_cells.py`, `test_schema_drift.py`) carry paired `history_tracked`/`temporal_class` call sites — no leftover un-paired `column_spec(...)` calls found.
  - The recipe-fixture doctor-kind "gains a tracked column" requirement's initial misattribution to `tests/recipes/_recipe_fixture.py` was confirmed reverted: `tests/recipes/_recipe_fixture.py`'s `_DOCTOR_COLUMNS` carries only the pre-existing `slice_only` `prop__name` — no orphaned tracked-column edit remains there. The correct fix lives in `tests/reader/_fixtures_build.py`'s `_history_series_doctor_columns` (`prop__specialty`, history-tracked, genesis-only per doctor) feeding `build_membership_intervals`, and `examples/recipes/corrupt/hard-deleted-parents/expect.yaml` churns exactly as specced (`impact: [C10]` → `[C6, C10]` on the `hard_delete_referenced_doctor` defect). No residue from the wrong first attempt.

**7b — spec → impl.** Compared signatures/docstrings/raises for every contract in spec.md against the diff: `TemporalClass`, `ColumnSpec.temporal_class`, `Sidecar.temporal_class` (including its `_column` helper), `TemporalClassUnavailableError`/`ColumnNotFoundError`, `_is_kind_tracked`, `_check_c11`'s converse clause, `_check_c13` (structural + genesis), the base-writer round-trip invariant, `drop_events`' emptied-series clause, and `prop_column`/`write_emit`. All match the spec verbatim in signature, docstring content (Args/Returns/Raises), and behavior — no divergence found in either direction (worse-than-spec or unjustified deviation).

**7c — impl → spec (spec-time miss audit).** Checked whether `prop_column`/`write_emit`, `PairKey`, and the new conformance helpers duplicate something the rest of the tree already had: `write_emit` genuinely consolidates what were previously N ad hoc `json.dumps`/hand-built dict sites (confirmed via the file list — 13+ call sites migrated); `PairKey` is a distinct, coarser-grained alias from the pre-existing `SeriesKey` (no `record_id`), not a redundant duplicate; the round-trippable-type constant and its C6 precedent are reused, not reinvented. No case found where the spec should have prescribed reuse of an existing helper instead of a new one.

## Recommendation

**APPROVED-WITH-NOTES**

No blockers. Three observations recorded (Gate 2 DRY consolidation candidate, Gate 5 coverage gap on a documented C13 exclusion branch, Gate 6 type-ignore density in one test adapter file) — all mergeable as-is; fix-vs-accept is the user's call.
