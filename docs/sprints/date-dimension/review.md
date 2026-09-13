# Sprint Review: date-dimension

**Date:** 2026-09-13
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# Future:`/`# TODO`, no bare-`pass` loop bodies, no inert self-renames in the diff. Every new public symbol (`date_key_expr`, `date_parse_expr`, `anchor_temporal_expr`, `compile_date_dimension_spec`, `check_date_refs_in_range`, `resolve_date_ref_source`, `render_date_ref_expr`, `DateRefSpec`, `DateDimensionConfig`, `DateRefOutOfRange`, `CalendarSource`) traced via `find_references`/`find_workspace_symbols` to a production caller, not just tests. |
| 2. Consistency / DRY | observations | 1 | See Findings — one pre-existing (not sprint-introduced) stale test name surfaced while reading a migrated file; no new duplicate helper found after loading `supplements.py`, `columns.py`, and `validation.py` as siblings. |
| 3. Test names | observations | 1 | Same pre-existing stale name (`test_manifest_v3_...`) noted under gate 2; every sprint-authored test name matched its body on sampling across `test_date_ref.py`, `test_date_dimension.py` (config/companion), `test_windowing.py`, `test_provenance.py`, `test_sql.py`, `test_anchor.py`. |
| 4. Test value | clean | 0 | No test-multiplication group found (parametrize used where the shape repeats, e.g. `test_anchor_temporal_expr_plus_alias...`, `test_date_parse_expr_plus_alias...`). `is not None` assertions sampled are all narrowing guards ahead of further attribute use (`outcome.window`, `anchor`) or are themselves the substantive NULL/non-NULL claim under test (`test_date_ref_instant_deactivated_at_null_for_active_record`), not stand-ins for a value pin. |
| 5. Coverage | clean | 0 | New file `src/fabulexa_forge/exporters/date_dimension.py`: 100%. Every other touched src file in scope: 90–100% (lowest: `windowing.py` 90%, `fk.py` 94%, pre-existing misses unrelated to this sprint's added lines). All well above the 85% floor. |
| 6. Type-ignore density | clean | 0 | Zero `# type: ignore` markers added in the diff. |
| 7. Spec ↔ codebase | clean | 0 | Every contract in spec.md § Contracts (config models, `_sql.py`, `anchor.py`, `errors.py`, the new `date_dimension.py`, `query_spec.py`, `dimensional/{columns,scd,validation,windowing,engine}.py`, `incremental/driver.py`, `playback/shaped.py`, `companion/{manifest,dictionary}.py`) checked line-by-line against the diff; signatures, docstrings, call order, and raise clauses match. The `_static_*` rename in `shaped.py` was explicitly left to implementer discretion by the spec, and reads as sanctioned generalization, not scope creep. No 7c finding — the new module reuses `supplements.py`'s mode-neutral placement pattern rather than reinventing it, and `resolve_date_ref_source` is factored once and reused at both its call sites rather than inlined twice. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty. |
| 9. Pre-commit | clean | 0 | `pre-commit run --files <41 changed files>` — all hooks passed (trim-whitespace, end-of-file, ruff, ruff format, mypy --strict, understand-bundles). Also ran separately over the two recipe YAML files and `state.yaml` — all passed. |
| 10. Demos | clean | 0 | All six `docs/sprints/date-dimension/demos/phase_*.py` ran twice, exit 0 each time, byte-identical stdout+stderr across both runs. |

## Findings

### Gate 2 / 3: Consistency and Test Names

- **finding 1** (observation): `tests/exporters/dimensional/test_export_supplements.py::test_manifest_v3_supplement_provenance_and_embedded_config` — the function name still says "v3" though the docstring and assertion were correctly updated by this sprint to `manifest_format_version == 5` (it was already stale at v4, before this sprint). This is pre-existing debt the sprint's migration touched but did not rename; not introduced by this sprint's authored code, and not a config-boundary or contract issue. Optional cleanup, not a blocker.

## Config-Boundary Review (primary focus)

Applied the scope test to every `.get(`, `or`-fallback, and `= None` default touched by the diff in `src/`:

- `DateRefSpec.source: str | None = None`, `from_: str | None = None`, `format: str | None = None`, `DateDimensionConfig` fields, `ExportConfig.date_dimension: DateDimensionConfig | None = None`, `ColumnDecl.date_ref: DateRefSpec | None = None` — all absence-detection optionals mirroring the pre-existing sibling modes (`fk`, `derived`, `lookup`, etc.); each is enforced by a `model_validator` that raises when the author's declared shape is incomplete or contradictory (`exactly_one_shape`, `range_ordered`, `date_dimension_requires_dimensional`, `date_refs_require_date_dimension`, `dim_date_name_reserved`). No fallback value is substituted anywhere in this chain — every missing/invalid case raises `ValidationError`/`ValueError` at load time, matching Principle #7 exactly.
- `table.references.get(name)` / `_date_ref_columns`'s `spec.references.items()` filter / `table.author_descriptions.get(column_name)` in `manifest.py` and `dictionary.py` — these key into structures the compiler itself stamps (`QuerySpec.references`, `TableReport.author_descriptions`), not author config directly; `None` on a miss means "this particular column doesn't reference anything" / "no override for this column," which is the correct per-column existence question, not an invented config value. Scope test: these are not values an author declares in YAML for date_dimension itself; they are derived per-column facts. Not a violation.
- `check_date_refs_in_range`'s bound values (`config.from_`, `config.to`) are read directly off the validated `DateDimensionConfig` with no `or`/default — the author's declared range is exactly what the guard checks.
- No instance of `grain = config.x or default` or an invented target/key/schema value was found anywhere in the diff.

**Conclusion: no config-boundary violations found.**

## Recommendation

**APPROVED-WITH-NOTES** — no blockers; one observation recorded (Gate 2/3, finding 1: a
pre-existing stale test-function name unrelated to this sprint's contract, not a defect
introduced here). Mergeable; the fix-vs-accept call on that observation is the user's,
not an auto-fix loop.

