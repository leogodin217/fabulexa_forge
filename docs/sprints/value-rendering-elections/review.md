# Sprint Review: value-rendering-elections

**Date:** 2026-08-21
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# Future:`/`# TODO:` in src/, no bare-`pass` bodies, no self-rename aliases added; every new public symbol (`render_decimal_expr`, `render_json_precision_expr`, `forge_json_precision`, `register_render_functions`, `build_decimal_expr`, `build_json_precision_expr`, `check_decimal_source_column`, `check_json_precision_source_column`, the five new election model classes, the five new error classes) traced via `find_references`/`find_definition` to a production caller, not just tests/demos. |
| 2. Consistency / DRY | observations | 1 | `_TYPED_ELECTION_SOURCE_GATES` + `_verify_typed_election_source_type` are byte-identical between `exporters/base/plan.py` and `exporters/source/plan.py`, and a narrower 2-entry `_STREAM_RENDER_SOURCE_GATES` variant repeats the same shape in `exporters/streaming/engine.py`. See Findings. |
| 3. Test names | clean | 0 | Sampled `test_value_election_plan.py` (source), `test_value_election_stream.py`, `test_events_render.py`'s migrated assertions — every name matches its body's actual assertions; no `test_X_order`/`test_X_deterministic`/`test_X_error` mismatches found. |
| 4. Test value | observations | 1 | Two groups of three structurally-parallel plan-gate tests (decimal/instant/json_precision × resolves/refused) in both `tests/exporters/source/test_value_election_plan.py` and its base counterpart are parametrize candidates. No weak-assertion violations found — every `is not None` / `row is not None` sampled is a type-narrowing step immediately followed by an exact-value assertion. |
| 5. Coverage | clean | 0 | `pytest --cov=src/fabulexa_forge --cov-report=term-missing`: 5003 passed, 18 skipped, TOTAL 98%. Every file this sprint touched is ≥89% (`exporters/dimensional/validation.py` lowest at 89%, a pre-existing 511-statement file; the sprint's own new functions in it, `check_decimal_source_column` / `check_json_precision_source_column`, fall entirely inside covered ranges). No file created under `src/` this sprint (all changes are modifications), so the "new file <85%" flag does not apply. |
| 6. Type-ignore density | clean | 0 | Zero `# type: ignore` markers added anywhere in the tests diff. |
| 7. Spec ↔ codebase | observations | 1 | 7b (spec→impl): every contract in spec.md § Contracts (the five election/spec models, the four `_sql.py` functions, the five error classes, all seven per-mode attach-point deltas) matches the implementation's signature, docstring, and raises clause. No divergence found. 7a: sprint notes read for all 7 phase commits; phase 3 and phase 6 notes explicitly record the base/streaming gate-table duplication as a deliberate decision under the pre-existing layer-direction invariant (matches Gate 2's finding — not a fresh miss). 7c (impl→spec): the same duplication is the one candidate for a "spec should have prescribed reuse" finding, but it followed established codebase precedent (`_verify_date_parse_source_varchar`/`_column_types` were already duplicated between `base/plan.py` and `source/plan.py` before this sprint) — the spec was consistent with existing convention, not a fresh miss. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty. |
| 9. Pre-commit | clean | 0 | `pre-commit run --all-files`: trim trailing whitespace, end-of-file fixer, check-yaml, check-toml, ruff (legacy alias), ruff format, mypy (strict, src), understand bundles (--check) — all Passed. |
| 10. Demos | clean | 0 | All 7 `docs/sprints/value-rendering-elections/demos/phase_{1..7}_*.py` ran twice via `uv run python`; every run exited 0; every pair of runs produced byte-identical stdout (`diff -q`). |

## Findings

### Gate 2: Consistency / DRY

- **finding 1** [`src/fabulexa_forge/exporters/base/plan.py` vs `src/fabulexa_forge/exporters/source/plan.py`] — `_TYPED_ELECTION_SOURCE_GATES: dict[str, tuple[str, type[ExportError], str]]` (the decimal/instant/json_precision source-type gate table) and its consuming function `_verify_typed_election_source_type` are word-for-word identical in both files. A narrower two-entry sibling, `_STREAM_RENDER_SOURCE_GATES` / `_verify_stream_render_source_type`, repeats the same shape a third time in `src/fabulexa_forge/exporters/streaming/engine.py`.
  Severity: observation, not a blocker. Verified via `git show <baseline>:...` that this exact pattern — `_verify_date_parse_source_varchar` and `_column_types` duplicated verbatim between `base/plan.py` and `source/plan.py` — predates this sprint. The module docstrings in both files explicitly declare a layer-direction invariant ("Never imports exporters.dimensional.*, exporters.source.*, or exporters.streaming.*" / vice versa), so a shared helper module is architecturally unavailable to either mode without a boundary change. The phase 3 and phase 6 sprint notes (`git notes --ref refs/notes/agent/sprint show 47201d5` / `fabd69a`) record this duplication as a considered decision ("Base election dispatch/gate helpers duplicated from source/* per the documented layer-direction invariant"; "Streaming carries its own numeric-only gate map per the documented layer-direction invariant"), not an oversight. Recorded here per the gate's instruction to surface literal duplication even when architecturally justified — a future architecture pass could promote a shared read-only gate-table module beneath all three modes, but that is out of this sprint's scope.

### Gate 4: Test value

- **finding 1** [`tests/exporters/source/test_value_election_plan.py:110-229`, mirrored in `tests/exporters/base/test_value_election_plan.py`] — `test_decimal_on_double_source_resolves` / `test_instant_on_bigint_source_resolves` / `test_json_precision_on_varchar_source_resolves` are structurally identical (build a table, elect the form, assert `table.render == ((key, Election(...)),)`), differing only in the election class, key, and column. The matching `test_*_on_non_*_source_refused` triad is the same shape under `pytest.raises`. Both are `@pytest.mark.parametrize` candidates (three iterations each, the gate-4 threshold).
  Severity: observation, not a blocker — each test asserts an exact value (never a weak `len(...) > 0` / bare `is not None` as its sole check) and each is independently readable; consolidating is a style improvement, not a correctness gap.

## Recommendation

**APPROVED-WITH-NOTES**

No blockers. Two observations recorded (Gate 2's cross-mode gate-table duplication — verified as a deliberate, precedented, notes-documented architectural decision, not an oversight; Gate 4's parametrize candidates in the plan-gate test triads). Mergeable as-is; fix-vs-accept on the observations is the user's call at the ACCEPT/FIX checkpoint.
