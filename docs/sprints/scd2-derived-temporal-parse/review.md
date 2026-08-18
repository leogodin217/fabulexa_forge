# Sprint Review: scd2-derived-temporal-parse

**Date:** 2026-08-18
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# TODO`/`# Future:` added; the two added `pass` bodies are the demos' `_discard_notice` no-op sink (a legitimate demo constant, not a config-boundary or future-scaffolding item); no self-rename aliases; every new public/production symbol (`validate_date_parse_format`, `date_parse_denoted_type`, `check_scd2_derived_source_untracked`) traced via `find_references` to a production caller, not just tests/demos. |
| 2. Consistency / DRY | observations | 1 | `scd.py`'s new `_resolve_source_column_type` duplicates the sidecar column-type-lookup loop already inline in `columns.py`'s `build_column_expr` (value_map branch) — see Findings. |
| 3. Test names | clean | 0 | Spot-checked new test names against bodies across `test_sql.py`, `test_scd.py`, `test_validation.py`, `test_models.py` — every name's claim (denotation, refusal rule, expression identity, constancy across versions) is what the body actually asserts. |
| 4. Test value | observations | 1 | Three near-identical `test_render_date_parse_expr_*_denotation_type` tests in `test_sql.py` are a parametrization candidate; no weak `len()>0` / bare-`is not None` assertions found in sprint-added test bodies outside one DB-cursor sanity guard. |
| 5. Coverage | observations | 2 | `_sql.py` 98% (2 new-code lines uncovered: the malformed-`%` branch and the final "must denote a complete date/time/both" catch-all); `scd.py` 99% (1 line uncovered: `_resolve_source_column_type`'s not-found→VARCHAR fallback). `validation.py`'s new function (`check_scd2_derived_source_untracked`) is fully covered; its overall-file gaps are all pre-existing, outside the diff. |
| 6. Type-ignore density | clean | 0 | Zero `# type: ignore` added in the sprint diff. |
| 7. Spec ↔ codebase | clean | 0 | Every Contracts-section signature/docstring/Raises in spec.md matches the implementation verbatim (`validate_date_parse_format`, `date_parse_denoted_type`, `render_date_parse_expr`, `DateParseSpec.format_denotes_a_temporal`, `build_scd2_column_expr_flag`, `check_scd2_column_mode_supported`, `check_scd2_derived_source_untracked`); both amended error message templates match the spec's table exactly; 7c — the `sidecar.columns()` linear-scan idiom `_resolve_source_column_type` follows is the package's pre-existing pattern (present in `validation.py` before this sprint too), so the spec proposing it is consistent with, not a miss against, the established idiom — see Gate 2 for the narrower near-duplicate with `columns.py`. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty. |
| 9. Pre-commit | clean | 0 | `pre-commit run --files <all 16 sprint-changed files>` — trim-whitespace, end-of-file, ruff, ruff-format, mypy-strict all Passed. |
| 10. Demos | clean | 0 | Both `phase_1_parse_family.py` and `phase_2_scd2_derived.py` run twice each, exit 0, byte-identical output both runs; printed output matches the spec's demo narrative (denotations, refusals, loud mismatch; derived-mode export + both refusals) exactly. |

## Findings

### Gate 2: Consistency / DRY

- **finding 1** [`src/fabulexa_forge/exporters/dimensional/scd.py:41-60`] — `_resolve_source_column_type` is a fresh 4-line loop (`for col_spec in sidecar.columns(source_table_name): if col_spec.name == column_name: return col_spec.type` … `return "VARCHAR"`) that duplicates the same lookup already inline in `src/fabulexa_forge/exporters/dimensional/columns.py`'s `build_column_expr` (`derived.value_map` branch, ~line 495-508). The two differ only in error handling: `columns.py`'s version wraps the scan in `try/except TableNotFoundError` and re-raises as `ExportError`; the new `scd.py` helper has no such guard. Severity: **observation** — the happy-path behavior is unchanged from this sprint's pre-existing `dict.get(..., "VARCHAR")` fallback (same silent-VARCHAR-on-miss semantics existed before this sprint, just as an inline dict build); this is a missed dedup opportunity, not a new correctness or config-boundary risk. Fix (if taken up): extract one `_resolve_sidecar_column_type(sidecar, table, name, default="VARCHAR")` helper shared by both modules, or accept the divergence as intentional (one path never sees a missing table, the other must).

### Gate 4: Test value

- **finding 1** [`tests/test_sql.py:444-462`] — `test_render_date_parse_expr_timestamp_denotation_type`, `test_render_date_parse_expr_time_denotation_type`, `test_render_date_parse_expr_date_denotation_type` are three tests whose bodies differ only in the literal value/format/expected-type triple (`_execute_date_parse(v, fmt)` → assert `sql_type == X` → assert `value == Y`). Severity: **observation** — a `@pytest.mark.parametrize` over `(value_str, date_format, expected_type, expected_value)` would collapse these to one test with three ids; not blocking since each assertion is exact-value (not a weak-assertion violation), just a multiplication candidate per the gate-4 heuristic.

### Gate 5: Coverage

- **finding 1** [`src/fabulexa_forge/_sql.py:301`, `:402`] — Two of the new `validate_date_parse_format`/`_date_parse_directives` error branches are unexercised by any sprint test: the malformed-`%` guard (a lone trailing `%` or similar) at line 301, and the final "must denote a complete date … a complete time … or both" catch-all at line 402 (reachable via a format with no recognized date or time directives at all, e.g. pure literal text or `%%`-only). Severity: **observation** — both are genuine load-time `ValueError` paths an author could realistically hit (a typo'd format with no directives), not `assert False`-style unreachable code; recommend a fixture for each in a follow-up.
- **finding 2** [`src/fabulexa_forge/exporters/dimensional/scd.py:60`] — `_resolve_source_column_type`'s not-found→`"VARCHAR"` fallback line is never hit by any sprint test (all fixture columns used in `build_scd2_column_expr_flag` tests are present in the sidecar). Severity: **observation** — low risk since the fallback is inherited unchanged from the pre-sprint `dict.get(..., "VARCHAR")` behavior, but worth a covering case now that the branch is a named, independently-testable function.

## Recommendation

**APPROVED-WITH-NOTES**

No blockers. Five observations recorded above (one DRY, one test-multiplication, three coverage) — none touch the config boundary, contract compliance, or correctness; all are mergeable as-is per the user's own accept/fix call.
