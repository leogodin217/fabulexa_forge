# Sprint Review: date-ref-scd-window

**Date:** 2026-09-19
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

Diff base: `a2cb94f` (`git merge-base HEAD date-ref-scd-window-work`). Two sprint
commits reviewed: `af05afe` (Phase 1: grammar, rendering, rules, windowing) and
`4bbc417` (Phase 2: report carriage, documentation channel, recipe).

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# TODO`/`# Future:`/bare-`pass` additions; the two new production symbols (`_table_window_bounds`, `check_date_ref_window_bound_on_scd2`) each trace to a real caller (`build_query_specs`, `validate_table`) via `find_references`; no inert self-renames in the diff. |
| 2. Consistency / DRY | observations | 2 | See Gate 2 below — a duplicated string literal and a duplicated 2-line dispatch, both small. |
| 3. Test names | clean | 0 | Read every new test's name/docstring/body across `test_date_ref.py`, `test_validation.py`, `test_windowing.py`, `test_provenance.py`, `test_query_spec.py`, `test_export_date_dimension.py`, companion `test_date_dimension.py`, and `incremental/test_date_dimension.py`; each asserts what its name claims (e.g. the "ordering pin" tests genuinely assert which rule fires first, the "deterministic" test genuinely compiles twice). |
| 4. Test value | clean | 0 | Exact-value assertions throughout (row keys, SQL fragments, `references`/`window_bounds` dict equality, manifest bytes). The only near-duplicate pair (`test_write_query_specs_{duckdb,csv}_arm_forwards_window_bounds_verbatim`) is exactly the spec-mandated "both arms" pair (2 tests, not ≥3) — not multiplication. No weak `len()>0`/`is not None`-only assertions found in the diff. |
| 5. Coverage | clean | 0 | Full run: `uv run pytest --cov=src/fabulexa_forge --cov-report=term-missing tests/config tests/exporters tests/incremental tests/playback tests/recipes` → 4069 passed, 10 skipped (pre-existing, unrelated: missing dataset bundles, a mutually-exclusive-config skip), exit 0. No new `src/` files were added (all sprint changes are modifications), so the "<85% new file" trigger doesn't apply. Every touched module is 91–100% covered, and I traced every uncovered line in `columns.py`, `engine.py`, `validation.py`, `windowing.py`, `dictionary.py`, `driver.py` back to pre-existing code the sprint didn't touch — none is a sprint-added branch. |
| 6. Type-ignore density | clean | 0 | Diff contains zero `# type: ignore` additions in `tests/*.py`. |
| 7. Spec ↔ codebase | observations | 1 | See Gate 7 below — a spec-time file-list miss (7c), no contract drift (7a/7b). |
| 8. Workspace | clean | 0 | `git status --porcelain` is empty; sprint notes confirm the one out-of-scope-file touch (`tests/playback/test_shaped_date_dimension.py`) was a deliberate, disclosed fix, not an accidental leftover. |
| 9. Pre-commit | clean | 0 | `pre-commit run --files <29 changed files>` → all hooks Passed (trim whitespace, end-of-file, check-yaml, ruff, ruff-format, mypy --strict). |
| 10. Demos | clean | 0 | Both `phase_1_bound_shape_render.py` and `phase_2_documented_export.py` run twice each, exit 0, byte-identical stdout across runs; printed output matches the demo's own narrated claims (family agreement, NULL on open version, same-day key sharing, delivery-class shift, all three refusal messages; README/manifest/window_bounds parity in Phase 2). |

Severity values:
- **clean** — gate found nothing.
- **observations** — gate found smells worth recording but no blocker.
- **blockers** — must fix before merge.

## Findings

### Gate 2: Consistency / DRY

- **finding 1** (observation): `src/fabulexa_forge/exporters/dimensional/windowing.py:145` and `:161` — the string `"is the SCD-2 valid_to bound, closed by the next version"` is written twice, once in the `derived.scd_window` branch and once in the new `date_ref.scd_window` branch of `_channel_variance`. The docstring explicitly calls these two branches "mirroring" each other, so the duplication is deliberate, but a shared module-level constant (or a tiny `_scd_window_variance(bound)` helper both branches call) would remove the risk of the two copies drifting if the message is ever edited in one place and not the other.
- **finding 2** (observation): `src/fabulexa_forge/exporters/dimensional/scd.py` — the two-line pattern `col_name = "version_start" if bound == "valid_from" else "version_end"` / `qualified_source = f'"{version_alias}"."{col_name}"'` appears once in the pre-existing `derived: scd_window` branch (lines ~127–130) and again, byte-identical apart from the extra bound resolution step, in the new bound-shape `date_ref` branch (lines ~135–140) of `build_scd2_column_expr_flag`. Same observation as finding 1: small, deliberate mirroring per the docstring, but a one-line helper (`_version_bound_column(bound) -> str`) would remove the duplicate.

Neither finding rises to a blocker: both are 1–2 line literal/logic duplicates inside the same function, not a duplicated helper elsewhere in the package, and both are explicitly called out as intentional mirroring in the shipped docstrings.

### Gate 7: Spec ↔ codebase

- **finding 1** (observation, 7c-adjacent — a phase-2 fallout, not a spec defect): `tests/playback/test_shaped_date_dimension.py` was modified (`test_dim_date_sits_after_declared_tables` now expects `dim_patient_status` in the table-order list) but this file is **not** in the spec's Phase 2 file table. The change is a legitimate, disclosed side effect of adding `dim_patient_status` to the shared `examples/recipes/date-dimension/config.yaml` recipe that this playback test also reads — the sprint notes for `4bbc417` record it explicitly as a gate-failure fix outside the spec's file list. Not a bug and not undisclosed, but the spec's Phase 2 "Files" table should have anticipated that any recipe-config edit fans out to every test that loads that same recipe, including ones outside the companion/dimensional test trees. Calibration note for the spec process, not a sprint defect.

No 7a (sprint-notes-vs-code) or 7b (spec-contract-vs-implementation) drift found: every contract in spec.md (`DateRefSpec.scd_window`, `resolve_date_ref_source`, `render_date_ref_expr`, `build_column_expr`, `resolve_carried_source_column`, `build_scd2_column_expr_flag`, `check_date_ref_window_bound_on_scd2`, `check_temporal_render_requires_anchor`, `_collect_value_read_sources`, `validate_table`, `_channel_variance`, `TableReport`/`QuerySpec.window_bounds`, `write_query_specs`, `_build_windowed_report`, `_table_window_bounds`, `build_query_specs`, `_DATE_REF_WINDOW_BOUND_DESCRIPTION_TEMPLATE`, `resolve_column_doc`) matches its implementation's signature, docstring, and raise behavior line for line.

## Config-Boundary Audit (primary focus)

Traced every new/changed default, `or`-fallback, `.get(key, ...)`, and None-check in the diff to its source:

- `DateRefSpec.scd_window: Literal["valid_from", "valid_to"] | None = None` — absence-detection default (mirrors the pre-existing `source`/`from_`/`format` pattern), immediately enforced by `exactly_one_shape`'s three-way `sum(...) != 1` check. Not a fallback: a missing/invalid shape is a load-time `ValueError`.
- `QuerySpec.window_bounds: Mapping[...] = field(default_factory=dict)` — matches the spec's explicit "internal runtime type with defaulted siblings; no caller changes" call-out (same pattern as the pre-existing `references` field), not an author-configurable value.
- `TableReport.window_bounds: Mapping[...]` — **no default**, exactly as the spec's Breaking Changes table requires; every one of the ~15 constructor call sites across `query_spec.py`, `driver.py`, and the eight migrated test files states it explicitly (verified via diff).
- `table.window_bounds.get(column_name)` in `dictionary.py::resolve_column_doc` — a presence check on a forge-computed dict (analogous to the pre-existing `table.author_descriptions.get(...)` two lines above it), not a default substituted for a missing author value.
- `check_date_ref_window_bound_on_scd2` / `check_temporal_render_requires_anchor` / `_collect_value_read_sources` / `_channel_variance` — each takes an explicit branch for `scd_window is not None` and raises or returns a specific value; no silent fallback path was introduced.

No config-boundary violations found.

## Recommendation

**APPROVED-WITH-NOTES** — no blockers; two Gate 2 observations and one Gate 7 observation recorded above. Mergeable as-is; fix-vs-accept on the three observations is the user's call.
