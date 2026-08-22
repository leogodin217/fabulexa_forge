# Sprint Review: scd2-per-version-renderings

**Date:** 2026-08-22
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | observations | 1 | No `# Future:`/`# TODO:` markers, no bare `pass`-only bodies, no self-rename aliases added. The new `_column_source_name`'s `return None` branch (scd.py:79) is unreachable from its sole call site given the caller's prior `scd_window`/`null` guards — matches its documented "callers pass only mode-gate-admitted ColumnDecls" contract, paired with an `assert` immediately after in the caller. Not flagged as scaffolding; noted as a coverage-adjacent observation (see Gate 5). |
| 2. Consistency / DRY | observations | 1 | New helper `_build_actor_emit` (test_scd2_renderings.py:81) is a near-duplicate of the pre-existing `_build_scd2_emit` (test_scd.py:209) in the same package — same DDL-create/insert/`write_emit` call order and shape, differing mainly in a base+extra column split and optional `enum_domains` support. Tier-2 sibling comparison surfaced this; see Findings. |
| 3. Test names | clean | 0 | Read every new/changed test's name, docstring, and body in test_scd2_renderings.py and the rewritten blocks of test_validation.py; each name accurately describes its assertions (e.g. `..._raises` tests all assert via `pytest.raises` with a `match`, `..._passes` tests assert no raise). No test-name lies found. |
| 4. Test value | clean | 0 | No test-multiplication group of ≥3 near-identical bodies; the 15 new tests in test_scd2_renderings.py each cover a distinct scenario (mode × tracked/constant × historical-failure). No weak `len(x) > 0` / `is not None` assertions in the diff — the one `a == b == c` chain (test_scd2_renderings.py:599-601) pins exact literal values on both sides, not a transitive-None pattern. |
| 5. Coverage | observations | 1 | Full-suite coverage on the three touched modules: `scd.py` 99% (1 line missed: 79, the same unreachable `_column_source_name` branch from Gate 1), `columns.py` 95% (misses are all pre-existing, untouched-by-diff lines: value_map bool/float literal branches, `build_elapsed_expr`'s exception path, `build_column_expr`'s lookup dispatch), `validation.py` 90% (misses are all pre-existing incremental-gate lines outside the sprint's touched hunks). No new file drops below 85%; no sprint-introduced error path is uncovered. |
| 6. Type-ignore density | clean | 0 | Zero `# type: ignore` markers added anywhere in the diff (grep over the full test diff). |
| 7. Spec ↔ codebase | observations | 1 | 7b (spec→impl): every contract in spec.md § Contracts matches the implementation verbatim — `_column_source_name`, `build_scd2_column_expr_flag`'s `tracked_props` swap, the five reshaped builders' `source_expr`/`table_label` signatures, `check_scd2_column_mode_supported`'s widened admissibility and verbatim error message (matches design doc § Validation Rules row exactly), and `check_scd2_derived_source_constant`'s clean deletion (no shim, no residual references via `find_workspace_symbols`). 7c (impl→spec): the spec proposed a wholly new `test_scd2_renderings.py` fixture builder without noting the near-identical `_build_scd2_emit` already in `test_scd.py` (same package) — see Gate 2; a spec-time miss to calibrate for future fixture-builder additions. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty — no untracked or uncommitted files. |
| 9. Pre-commit | clean | 0 | `pre-commit run --files <all 9 touched files>` — trim-trailing-whitespace, end-of-file-fixer, ruff (legacy alias), ruff format, mypy (strict, src) all Passed; yaml/toml/understand-bundles hooks skipped (no matching files). |
| 10. Demos | clean | 0 | `docs/sprints/scd2-per-version-renderings/demos/phase_1_per_version_renderings.py` run twice via `uv run python`; both runs exit 0 with byte-identical stdout (`diff` empty) — per-version decimal/value_map rendering, colliding-decimal collapse, election-invariant version structure, and the widened ordinal-refusal message all verified deterministic. |

## Findings

### Gate 2: Consistency / DRY

- **finding 1**: `tests/exporters/dimensional/test_scd2_renderings.py:81` (`_build_actor_emit`) duplicates the shape of `tests/exporters/dimensional/test_scd.py:209` (`_build_scd2_emit`) — both: connect to `run.duckdb`, `_create_ddl`/insert `records__actor` and `history` rows in the same order, call `write_emit` with the same `_table_spec(...)` pair and the same `branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}]` literal, and `return tmp_path`. The two differ only in: (a) `_build_actor_emit` splits columns into a fixed `_actor_base_columns()` prefix + caller-supplied `prop_columns`, where `_build_scd2_emit` takes the full column list; (b) `_build_actor_emit` accepts an optional `enum_domains` extra. Estimated token overlap well above the ~70% duplicate threshold.
  Severity: observation (test-code hygiene, not a correctness or config-boundary issue).
  Suggested fix (for the user's call, not prescribed here): extend `_build_scd2_emit` with an optional `enum_domains` parameter (and/or a `prop_columns`-only calling convention) and reuse it from the new file, or promote a shared actor-emit builder into `tests/exporters/_emit_fixtures.py` alongside the existing entity/patient builders.

### Gate 7: Spec ↔ codebase

- **finding 1** (7c, spec-time miss): spec.md § Phase 1 prescribed a brand-new `test_scd2_renderings.py` fixture builder for a `records__actor` + `history` emit without cross-referencing `test_scd.py`'s pre-existing `_build_scd2_emit`, which builds the identical fixture shape (see Gate 2 finding 1). The implementer built exactly what the spec specified faithfully; the miss is at spec-authoring time, not in this sprint's code. Calibration note for future spec passes: when a new test file's fixture scenario overlaps an existing file in the same package, the spec should name the existing helper and prescribe extending/reusing it.

## Recommendation

**APPROVED-WITH-NOTES** — no blockers found across all 10 gates. Contract compliance (spec → impl) is exact, including verbatim error-message text and the documented breaking-change signature swaps. The config boundary is clean: no defaulted/`or`-fallback config-sourced values were introduced or found in the diff — `is_tracked`/`tracked_props` resolution is structural derivation from the sidecar (a sourced value, not an invented one), and the pre-existing `resolve_source_column_type`'s `"VARCHAR"` fallback is untouched by this sprint. Pre-commit, the full test suite (5010 passed, 18 skipped), and both demo runs are clean. Two observations are recorded (Gate 2 / Gate 7c, both pointing at the same DRY duplication) for the user's own fix-vs-accept decision; neither blocks merge.
